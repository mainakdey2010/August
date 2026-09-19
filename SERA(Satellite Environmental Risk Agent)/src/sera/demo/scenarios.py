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
]
