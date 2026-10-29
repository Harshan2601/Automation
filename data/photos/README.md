# Customer photo pools

Each customer gets a separate directory under `data/photos/<customer-uuid>/` when provisioned.

Copy only that customer's genuine, approved-to-use images into that directory (`.jpg`, `.jpeg`, `.png`). Add a sidecar text file to describe each image, for example:

- `team_event.jpg`
- `team_event.jpg.txt` containing `The customer speaking with colleagues at an internal team event; no client logos visible.`

Descriptions help the AI select an appropriate image. The system does not inspect image pixels with a vision model; it relies on the provided descriptions and filenames. Do not put one customer's photos into another customer's folder.
