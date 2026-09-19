"""Real historical events; selected demo asset points are NOT verified facilities."""
from sera.demo.models import Region

SCENARIOS = [
    Region(
        region_id='sindh-flood-2022', name='Sindh flood — September 2022',
        asset_label='Illustrative monitoring point near Sehwan (not a verified client asset)',
        longitude=67.85, latitude=26.43, radius_km=5,
        before={'start':'2022-06-01','end':'2022-06-30'},
        after={'start':'2022-09-01','end':'2022-09-20'}, as_of='2022-09-20',
        indices=['NDWI','MNDWI','NDVI'], baseline_years=3,
        event_context='Retrospective surface-water change during the 2022 Pakistan floods. '
                      'Review newly wet areas separately from permanent water. The selected point is illustrative; '
                      'a positive index change does not prove asset damage or flood depth.',
        sources=['https://www.adb.org/projects/57323-001/main'],
    ),
    Region(
        region_id='lahaina-fire-2023', name='Lahaina fire — August 2023',
        asset_label='Illustrative Lahaina town monitoring point (not a verified client asset)',
        longitude=-156.675, latitude=20.88, radius_km=3,
        before={'start':'2023-07-15','end':'2023-08-08'},
        after={'start':'2023-08-09','end':'2023-08-31'}, as_of='2023-08-31',
        indices=['NBR','NDVI'], baseline_years=3,
        event_context='Retrospective burn-related change around the August 8, 2023 Lahaina fire. '
                      'dNBR is pre-fire NBR minus post-fire NBR on jointly valid pixels. '
                      'Positive dNBR is a review signal, not a calibrated building-damage estimate.',
        sources=['https://www.fema.gov/disaster/4724'],
    ),
    Region(
        region_id='nepal-gyirong-disaster-2026', name='Nepal–Gyirong disaster — August 2026',
        asset_label='User-supplied Gyirong Port customs monitoring point (facility boundary unverified)',
        longitude=85.3777777778, latitude=28.2797222222, radius_km=3,
        before={'start':'2026-07-26','end':'2026-08-26'},
        after={'start':'2026-08-27','end':'2026-09-19'}, as_of='2026-09-19',
        indices=['NDWI','MNDWI','NDVI'], baseline_years=5,
        event_context='Retrospective optical change around the August 26, 2026 Gyirong–Rasuwa disaster. '
                      'This bounded port-area replay does not reconstruct the upstream source or full runout. '
                      'Cloud, snow, terrain shadow and debris can obscure water signals; absence of a '
                      'positive water-index change does not rule out impact. The user-supplied point '
                      'does not establish a facility footprint, damage, cause or warning lead time.',
        sources=['https://www.stimson.org/2026/a-cascading-disaster-on-the-china-nepal-border-what-to-know-about-the-august-2026-rasuwa-flood/'],
    ),
    Region(
        region_id='upper-assam-flood-2026', name='Upper Assam floods — July–August 2026',
        asset_label='Illustrative Sivasagar-area monitoring point (not a verified client asset)',
        longitude=94.63, latitude=26.98, radius_km=5,
        before={'start':'2026-06-01','end':'2026-07-01'},
        after={'start':'2026-07-22','end':'2026-08-15'}, as_of='2026-08-15',
        indices=['NDWI','MNDWI','NDVI'], baseline_years=5,
        event_context='Retrospective surface-water and vegetation change during the 2026 Assam monsoon floods. '
                      'This Sivasagar-area sample lies within the requested Upper Assam region; it is not '
                      'a statewide flood extent. Separate permanent water and seasonal paddy inundation '
                      'from possible flood change. Cloud gaps or recession before clear acquisitions '
                      'may hide the flood peak. Index changes do not establish damage or water depth.',
        sources=['https://www.theguardian.com/global-development/2026/aug/14/india-assam-climate-disaster-floods-brahmaputra-homeless-deaths'],
    ),
]
