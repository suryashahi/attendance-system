# Attendance — face + live-location attendance (Vercel)

A web app for classroom attendance. The teacher pins the classroom once,
enrolls each student with a one-time face scan, then starts a session:
the board shows a QR code that rotates every 60 seconds, and students
scan it with their phones, take a selfie, and share their location.
The server verifies the current code, the face match, and the distance
from the classroom, and logs every attempt in a register that exports
to CSV or Excel.

Face recognition runs entirely in the browser (face-api.js, bundled in
`public/static`). Only 128-number face descriptors are sent to and
stored on the server — never photos. Location comes from the device OS,
so it is evidence, not proof; the rotating board code is what proves
presence in the room. A static QR with a 10-minute window is available
as a fallback for rooms with no display.

## Deploy on Vercel

1. Push this folder to a GitHub repository.
2. In the Vercel dashboard, import the repository (Add New → Project).
   No build settings are needed — `vercel.json` and `requirements.txt`
   are picked up automatically.
3. In the project, open **Storage → Create Database → Postgres** and
   connect it to the project. Vercel sets the `POSTGRES_URL`
   environment variable automatically.
4. Deploy (or redeploy, if the first deploy ran before the database
   existed). The app creates its tables on first start.

That's it — the app is live at your Vercel URL, served over HTTPS, so
phone cameras and location work without any certificate setup.

## How it works

1. **Classroom** — pin the classroom location on the map once
   (geofence radius 30–50 m, default 40 m).
2. **Students** — enroll each student once with a face scan. Only the
   128 face numbers are stored.
3. **Session board** — start a session; the board shows the rotating
   QR code and 6-digit code (or a static code for 10 minutes).
4. **Check in** — students scan the QR, take a selfie, and share
   location. Code, face match, and distance are all checked.
5. **Register** — every attempt is logged with its face-match score
   and distance; export to CSV or XLSX for Excel.

## Local development

Needs a Postgres database. Point `POSTGRES_URL` at it and run:

```bash
pip install -r requirements.txt
POSTGRES_URL=postgresql://user:pass@localhost:5432/attendance python app.py
```

Then open http://localhost:5000. Note: phone cameras and geolocation
require HTTPS, so test those on the deployed Vercel URL.

## Notes

- The face model weights load from the jsDelivr CDN and the fonts from
  Google Fonts, so the first page load needs internet (the hosted app
  requires it anyway). Face matching itself still runs in the browser.
- Timestamps are recorded in server time (UTC on Vercel).
- Data lives in Postgres: classrooms, students (descriptors only),
  sessions, and attendance attempts. Deleting the database deletes
  everything.
