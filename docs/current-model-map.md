# Current model map

The **Models** tab shows the current over a venue as a moving map: colour is
speed, and streaks drift the way the water goes. The sailor can run through
the day with the slider, tap the water to read the current at a point, and
switch between current models. Each model carries a score: how far it was
from the readings taken on the water over the last 24 hours or the last 7
days (a setting in the model list).

## Who sees it

Only accounts the server allows. The server's answer to
`GET /api/mobile/preferences` carries `entitlements.currentModels`; the tab
appears when it is true (`AuthStore.canSeeCurrentModels`). The same rule is
applied again on every request for a map, so hiding the tab is a courtesy,
not the lock. Who is on the list is a server setting
(`CURRENT_MODELS_ALLOWED_EMAILS` in the portal); nothing in this repository
names an account.

## Where the maps come from

`model/` builds the models and `python -m currentmodel.publish` packs each
one as a small file per UTC day (hourly frames on a regular grid) and
uploads it to the portal, which stores the files privately:

| Request (under `/api/current-models/`) | Answer |
|---|---|
| `index.json` | the venues that have been published |
| `<venue>/manifest.json` | the models, and the days each has a map for |
| `<venue>/<model>/<day>.bin` | one model's maps for one day |
| `<venue>/validation` | each model scored against readings |

The file format is described at the top of `model/src/currentmodel/publish.py`
and read by `CurrentDayField`. Each day's map is built the evening before
from forecasts, so a score compares a reading with what the model said
before the day began.

## The code

| File | What it holds |
|---|---|
| `CurrentModels/CurrentModelManifest.swift` | the manifest, the grid, UTC days |
| `CurrentModels/CurrentField.swift` | reading a day's file; the current at a place and time |
| `CurrentModels/CurrentColorScale.swift` | speed to colour; the speed map as a picture |
| `CurrentModels/CurrentParticles.swift` | the drifting streaks, and how they keep off the land |
| `CurrentModels/CurrentMapRegion.swift` | what the map shows; the zoom buttons' arithmetic |
| `CurrentModels/CurrentValidation.swift` | the scores the server returns |
| `Services/CurrentModelStore.swift` | downloads, the on-disk cache, the choices |
| `Views/CurrentModelMapView.swift` | the screen |
| `Views/CurrentFieldOverlay.swift` | drawing over the map; the colour key |
| `Views/CurrentModelSelectorView.swift` | the model list with scores |

Everything under `CurrentModels/` uses Foundation only and is covered by
`CurrentCoachTests/CurrentModelTests.swift`.

The map zooms by pinch, and by the plus and minus buttons at its lower right, which halve or double the view; the third button brings the whole venue back.

No streak crosses the coastline the manifest carries. The streaks follow the current carried up to that coastline; one that meets the land runs along the shore, and one with nowhere to go fades out where it is. The published maps do their part as well: close to land the publisher takes out the part of every model's current that crosses the shore.

The map is kept north-up and flat (pan and zoom only) so that the picture
and the streaks can be placed from two corners of the view.
