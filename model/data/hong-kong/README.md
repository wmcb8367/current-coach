# Hong Kong depth data

## `admiralty-937-soundings.csv`

456 soundings read by eye from British Admiralty chart 937, *Eastern
Approaches to Hong Kong*, 1:50,000, compiled "from the latest information in
the Hydrographic Department, 1960", last corrected 1962. The scan is free to
view at the National Library of Australia (Loke Wan Tho map collection,
item 34): <https://nla.gov.au/nla.obj-714153570/view>. Admiralty charts are
UK Crown copyright, which runs 50 years from publication, so a 1962 chart is
out of copyright.

They cover about 22.22 to 22.34 N and 114.255 to 114.397 E: the race area
east of Clear Water Bay, the Ninepins, Clear Water Bay, Tathong Channel and
the mouth of Port Shelter.

Columns:

- `lat`, `lon`: WGS84 as far as the georeference allows (see below).
- `depth_m`: metres below mean sea level, which is what the model wants.
  It is the chart depth plus 1.4 m (mean sea level above chart datum at
  Tai Miu Wan is 1.44 m).
- `fathoms`, `feet`: as printed. Under 11 fathoms the chart adds feet.
- `below_chart_datum_m`: the chart depth in metres.
- `scan_x`, `scan_y`: where the figure sits on the scan, in pixels of zoom
  level 13 (a quarter of full size), so a reading can be checked.

Georeference: the chart's border gives one minute of longitude every 201.9
pixels from 114 deg 16' at x = 698, and one minute of latitude every 218.2
pixels from 22 deg 25' at y = 467. Positions were then moved 40 pixels east
and 18 south (about 200 m and 80 m) to put the Waglan and Cape D'Aguilar
lighthouses where they are today. Expect positions to be good to 200 to
400 m: fine for 200 m model cells offshore, rough along the shore.

Use it with the build:

```
python -m currentmodel.build --lat 22.28 --lon 114.36 --width-km 44 --height-km 38 \
    --depth-logs data/hong-kong/admiralty-937-soundings.csv --out hong-kong
```

No `--tide-station`: the depths are already below mean sea level.

The seabed here is mud and sand in 15 to 30 m of water and has changed
little in 60 years away from dredged channels and reclamations, but a
1960 survey is still a 1960 survey.
