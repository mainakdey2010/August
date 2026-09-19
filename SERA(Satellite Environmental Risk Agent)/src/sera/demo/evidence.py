"""Paired-pixel change summaries; uncalibrated review signals, never probabilities."""
import math
import statistics


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def summarize(region, scan_id, data):
    evidence=[]
    gaps=list(data.get('missing',[]))
    for scope in ['region','asset_buffer']:
        for idx in region.indices:
            samples=[]
            total_area=0
            for f in data['features']:
                p=f['properties']
                # An intersecting H3 cell is not the exact asset boundary: disclose this aggregation.
                area=p.get('area_m2',0) if scope=='region' else p.get('asset_overlap_m2',0)
                if not finite(area) or area <= 0:
                    continue
                total_area+=area
                coverage=p.get(idx+'_coverage',0)
                if not finite(coverage) or coverage < 0.5:
                    continue
                if not all(finite(p.get(idx+'_'+s)) for s in ['before','after','delta']):
                    continue
                samples.append((p,area*coverage))
            if not samples:
                gaps.append(f'{scope}/{idx}: insufficient jointly clear before/after pixels')
                continue
            weights=sum(w for _,w in samples)
            if total_area <= 0 or weights/total_area < 0.5:
                gaps.append(f'{scope}/{idx}: less than 50% jointly usable area')
                continue
            average=lambda key:sum(p[key]*w for p,w in samples)/weights
            before,after,delta=[average(idx+'_'+s) for s in ['before','after','delta']]
            history=[]
            for label in sorted(data['manifest']):
                if not label.startswith('baseline_'):
                    continue
                year=label.split('_')[-1]
                key=idx+'_baseline_'+year
                good=[(p,w) for p,w in samples if finite(p.get(key)) and p.get(idx+'_baseline_coverage_'+year,0) >= .5]
                if good:
                    history.append({'year':int(year),'mean':sum(p[key]*w for p,w in good)/sum(w for _,w in good)})
            # Yearly composites are samples; do not label pixel variance temporal variance.
            vals=[h['mean'] for h in history]
            sd=statistics.stdev(vals) if len(vals)>=3 else None
            z=(after-statistics.mean(vals))/sd if sd and sd>=0.02 else None
            direction=-delta if idx in ('NBR','NDVI') else delta
            evidence.append({'evidence_id':f'{scope}-{idx.lower()}', 'index':idx,'scope':scope,
                'before':before,'after':after,'delta_after_minus_before':delta,
                'dnbr':-delta if idx=='NBR' else None,
                'usable_area_fraction':min(1,weights/total_area) if total_area else 0,
                'cell_count':len(samples),'history':history,'baseline_year_count':len(vals),
                'z_score':z,'change_review_signal':direction>=0.15,
                'signal_rule':'Directional paired index change >= 0.15 (demo rule, not calibrated)',
                'baseline_status':'available' if z is not None else 'insufficient years or variability'})
            if z is None:
                gaps.append(f'{scope}/{idx}: seasonal z-score unavailable (requires >=3 years and stddev >=0.02)')
    return {'scan_id':scan_id,'asset_id':region.region_id,'asset_label':region.asset_label,
        'as_of':str(region.as_of),'mode':'retrospective_reconstruction','synthetic':False,
        'before':region.before.model_dump(mode='json'),'after':region.after.model_dump(mode='json'),
        'evidence':evidence,'data_gaps':gaps,'source_manifest':data['manifest'],
        'limitations':['Historical acquisition cutoff only; original operational availability is not proven.',
            'Index changes are not event probabilities, flood depth, building damage, or advance warning.',
            'Cloud-masked window composites are not single-date observations.',
            'Asset buffer summaries use intersecting H3 cell means, weighted by overlap; not exact footprint measurements.',
            'Yearly seasonal composite samples can have differing clear pixel support.',
            'Scenario asset coordinates are illustrative until an owner verifies a real asset.'],
        'event_context':region.event_context,'sources':region.sources}
