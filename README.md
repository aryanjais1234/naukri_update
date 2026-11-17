# Naukri Update

A standalone local app for Naukri resume headline rotation and resume refresh. It reuses the browser automation and update flows from JobPilot while keeping its database, browser profile, uploads, and schedules in this folder.

## Start on Windows

Open two PowerShell terminals.

### Backend

```powershell
cd F:\jobpilot-ai\naukri-updater\backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

### Frontend

```powershell
cd F:\jobpilot-ai\naukri-updater\frontend
npm ci
npm run dev
```

Open [http://127.0.0.1:5174](http://127.0.0.1:5174).

## Use

1. Select **Launch browser**, then **Open Naukri profile**. Sign in in this new browser profile; it does not use the JobPilot browser session.
2. Add 3 or 4 distinct resume headlines, select **1, 5, 15, 45, or 60 minutes**, and save. Start auto-update to apply the first headline immediately and continue on the selected schedule.
3. In the resume panel, enter your name and total experience, save the details, choose a PDF, DOCX, or TXT resume, and upload it. Select the resume refresh interval (the existing flow supports 1, 3, 6, or 24 hours), save it, then start refresh.

Keep the backend terminal running while schedules are active. The resume source file stays unchanged; uploads use a dated copy. Both automations require an authenticated Naukri session and pause on a detected error.

## Separate local data

Runtime data is created under this project's `data/` folder, including `naukri_updater.db`, `naukri-chrome-profile/`, uploaded resumes, and scheduler JSON files. Nothing in the parent JobPilot project is configured as a data location.
