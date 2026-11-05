# Where Cleveland's public trash cans are

An interactive map of the city's public street receptacles, shaded by ward and
plotted on aerial imagery. Built for Signal Cleveland.

**Live: https://eesedla.github.io/public-can-locations/**

`public-trashcan-locations/index.html` is the map: one self-contained file,
nothing to install, drop it on any static host. The `index.html` at the repo
root only forwards to it, because GitHub Pages serves a site from the root and
the build writes each map into its own folder.

`public-trashcan-locations/wordpress-embed.html` is the parent-page snippet
that frames it and keeps the iframe at the right height. Point its iframe at
the map's own path, not at the site root:

    https://eesedla.github.io/public-can-locations/public-trashcan-locations/

## Reading the map

Citywide it is a choropleth: each of the 15 wards shaded by how many public
cans it holds, in four quantile classes, so roughly four wards fall in each
color band. The side table ranks the wards and also gives cans per 10,000
residents, which controls for ward size — though because redistricting draws
wards to near-equal population, that ranking stays close to the raw count.

Pressing a ward zooms to it, drops the shading, and puts every can on the bare
imagery. Pressing a can goes down to the block it sits on. Several cans at one
spot draw as a single marker carrying the count.

## The data

**834 cans, 830 of them inside a ward.** Source: the city's public receptacle
list as of November 4, 2026, geocoded with Geocodio
(`data/public receptacles.csv`). Ward boundaries are the 2026 wards
(Ord. No. 1-2025) from City of Cleveland Open Data. Population is the Census
Bureau's American Community Survey 2020–2024 five-year estimates, aggregated by
ward — estimates, so each carries a margin of error of roughly ±6 percent, and
small gaps between adjacent wards are not meaningful.

Three things worth knowing before quoting a number off this map:

**One row is one can.** The city's list has 835 rows but only 483 distinct
coordinates, because a busy corner holds several cans. Every row is counted. If
the duplicates turn out to be double entries rather than separate cans, the
citywide total drops to about 517 and every ward figure moves with it.

**24 rows were re-geocoded by hand.** Geocodio misplaced them, in a few cases
badly: five different Detroit Avenue corners collapsed onto one point because
the matcher dropped the street number, "Euclid Ave & E 90th St" matched I-90
about 3.9 km west, and "Miles Ave & E 135th St" landed 5 km south of the city.
Each correction is recorded with its before, its after and how it was resolved
in `data/public-trashcan-locations-geocode-fixes.json`, mostly against
OpenStreetMap. One row — a bare house number, "16800", with no street — was
dropped as unplaceable.

Rows whose coordinates look soft but not wrong, such as a street-center
fallback that still lands on the right street in the right ward, were left
alone rather than guessed at. Locations are geocoded from addresses and
intersections, so any one of them may be off by a few dozen feet.

**Four cans sit outside every ward polygon** — two at E 105th St & Martin
Luther King Jr Dr, two at Lee Rd & S Miles Rd. Both were checked against
OpenStreetMap and the coordinates are right; they just fall on the city
boundary. That is the gap between "834 cans" and "830 mapped".

## Rebuilding

```
python3 build_maps.py
```

No dependencies beyond the standard library. It reads the CSV and the ward
polygons, joins each can to a ward by point-in-polygon, bins the wards into
color classes and writes the map.

Inputs:

| path | what it is |
| --- | --- |
| `data/public receptacles.csv` | the city's list, geocoded. Replace this to update the map. |
| `data/public-trashcan-locations-geocode-fixes.json` | hand corrections, keyed on the exact address string |
| `assets/wards.geojson` | 2026 ward boundaries |
| `assets/template-points.html` | the map itself — real HTML, CSS and JS, edit it directly |
| `assets/wordpress-embed-template.html` | the parent-page embed snippet |

Outputs, all regenerated on every run:

| path | what it is |
| --- | --- |
| `public-trashcan-locations/index.html` | the map |
| `public-trashcan-locations/wordpress-embed.html` | the embed snippet |
| `public-trashcan-locations/by_ward.csv` | the ward summary table behind the map |
| `public-trashcan-locations/data.csv` | the cleaned location table: address, coordinates, cans at that spot, ward |

`data.csv` is a build **output**, not an input. Editing it by hand will not
survive the next run — corrections belong in the geocode-fixes file.

The script also knows about two Cleveland 311 maps that are not in this repo.
It skips any dataset whose CSV is missing, so they cost nothing here.

## Basemap

MapLibre GL JS 4.7.1 with Esri World Imagery as a raster source, which has real
tiles through zoom 19 over Cleveland where USGS imagery caps at 16. The
attribution string — *Imagery: Esri, Maxar, Earthstar Geographics* — is Esri's
condition of use and has to stay on the map.
