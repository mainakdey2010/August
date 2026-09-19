"""Real Sentinel-2 historical composites and H3 reductions; never fabricates gaps."""
from __future__ import annotations
import calendar
import math
import os
from datetime import date

COLLECTION = 'COPERNICUS/S2_SR_HARMONIZED'
BANDS = {'NDVI':('B8','B4'), 'NDWI':('B3','B8'), 'MNDWI':('B3','B11'), 'NBR':('B8','B12')}
SCALE = 20


def bounds(region):
    dy = region.radius_km / 111.195
    dx = dy / math.cos(math.radians(region.latitude))
    result = [region.longitude-dx, region.latitude-dy, region.longitude+dx, region.latitude+dy]
    if result[0] < -180 or result[2] > 180:
        raise ValueError('Antimeridian regions require a split geometry')
    return result


def shift_year(d, year):
    return date(year, d.month, min(d.day, calendar.monthrange(year, d.month)[1]))


def baseline_windows(region):
    return [(shift_year(region.after.start, year), shift_year(region.after.end, year + region.after.end.year - region.after.start.year))
            for year in range(max(2019,region.after.start.year-region.baseline_years), region.after.start.year)]


def collect(region, progress=lambda stage: None):
    import ee
    import h3
    project = os.environ.get('GCP_PROJECT') or os.environ.get('GOOGLE_CLOUD_PROJECT')
    if not project:
        raise RuntimeError('GCP_PROJECT is required')
    ee.Initialize(project=project)
    ee.data.setDeadline(180000)
    box = bounds(region)
    geom = ee.Geometry.Rectangle(box, geodesic=False)
    asset = ee.Geometry.Point([region.longitude,region.latitude]).buffer(region.asset_radius_m)
    coords = [[box[0],box[1]],[box[2],box[1]],[box[2],box[3]],[box[0],box[3]],[box[0],box[1]]]
    # One extra ring includes cells whose centres fall just outside the region.
    cells = set(h3.geo_to_cells({'type':'Polygon','coordinates':[coords]}, 8))
    for cell in list(cells):
        cells.update(h3.grid_disk(cell, 1))
    if len(cells) > 1000:
        raise ValueError('Demo region exceeds 1000 H3 cells; reduce radius')
    features=[]
    for cell in sorted(cells):
        ring=[[lon,lat] for lat,lon in h3.cell_to_boundary(cell)]
        ring.append(ring[0])
        clipped=ee.Geometry.Polygon([ring]).intersection(geom, 1)
        features.append(ee.Feature(clipped, {'h3_cell':cell,'area_m2':clipped.area(1),
                                             'asset_overlap_m2':clipped.intersection(asset,1).area(1)}))
    fc=ee.FeatureCollection(features).filter(ee.Filter.gt('area_m2', 1))
    manifest={}

    def composite(start, end, label):
        raw=(ee.ImageCollection(COLLECTION).filterBounds(geom)
             .filterDate(str(start), str(end)).sort('system:time_start'))
        count=raw.size().getInfo()
        if count > 120:
            raise ValueError(f'{label}: more than 120 scenes; narrow the window')
        manifest[label]={'start':str(start),'end_exclusive':str(end),'scene_count':count,
                         'source_ids':raw.aggregate_array('system:index').getInfo(),
                         'acquisition_ms':raw.aggregate_array('system:time_start').getInfo()}
        if count == 0:
            return None

        def prepare(img):
            # QA60 is fully masked in 2022–2024: use SCL consistently across years.
            scl=img.select('SCL')
            mask=scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6))
            sr=img.select(['B2','B3','B4','B8','B11','B12']).multiply(0.0001).updateMask(mask)
            common=sr.mask().reduce(ee.Reducer.min())
            sr=sr.updateMask(common)
            products=[]
            for name in region.indices:
                a,b=BANDS[name]
                den=sr.select(a).add(sr.select(b))
                products.append(sr.select(a).subtract(sr.select(b)).divide(den)
                                .updateMask(den.abs().gt(1e-6)).rename(name))
            return sr.addBands(ee.Image.cat(products)).copyProperties(img,['system:time_start'])
        return raw.map(prepare).median().clip(geom)

    before=composite(region.before.start,region.before.end,'before')
    after=composite(region.after.start,region.after.end,'after')
    if before is None or after is None:
        return {'type':'FeatureCollection','features':[], 'manifest':manifest,
                'bounds':box,'missing':['No scenes in before or after window'], 'images':{}}
    progress('historical_baseline')
    baseline=[]
    for start,end in baseline_windows(region):
        img=composite(start,end,f'baseline_{start.year}')
        if img is not None:
            baseline.append((start.year,img))

    layers=[]
    for name in region.indices:
        a,b=before.select(name),after.select(name)
        valid=a.mask().And(b.mask())
        layers.extend([a.updateMask(valid).rename(name+'_before'), b.updateMask(valid).rename(name+'_after'),
                       b.subtract(a).rename(name+'_delta'),valid.unmask(0).clip(geom).rename(name+'_coverage')])
        for year,img in baseline:
            layers.append(img.select(name).rename(f'{name}_baseline_{year}'))
            layers.append(img.select(name).mask().unmask(0).clip(geom).rename(f'{name}_baseline_coverage_{year}'))
    progress('h3_reduction')
    reduced=ee.Image.cat(layers).reduceRegions(collection=fc,reducer=ee.Reducer.mean(),
                                             scale=SCALE,crs='EPSG:6933',tileScale=4).getInfo()
    result={'type':'FeatureCollection','features':reduced['features'], 'manifest':manifest,'bounds':box,
            'missing':[], 'images':{}, 'scale_m':SCALE,'collection':COLLECTION}
    progress('satellite_previews')
    # Thumbnails are persisted, so no short-lived map token is saved as a demo result.
    import requests
    for label,img in [('before',before),('after',after)]:
        url=img.getThumbURL({'region':geom,'dimensions':800,'format':'png',
                            'bands':['B4','B3','B2'],'min':0,'max':0.3,'crs':'EPSG:4326'})
        response=requests.get(url,timeout=180)
        response.raise_for_status()
        if not response.content.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Earth Engine preview did not return a PNG')
        result['images'][label]=response.content
    return result
