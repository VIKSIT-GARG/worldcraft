# Run WORLDCRAFT

## Start the application

Use Python 3.12+ and Node.js 20.19+ or 22.12+. From the repository root:

```bash
./run.sh
```

Open **http://localhost:8000**. The launcher installs the backend dependencies,
builds the frontend and starts FastAPI.

Select **Local demo · no AI** to run the deterministic city workflow. Select
**Vertex AI Gemini · live** for contextual Hindi, Hinglish and English planning.

## Manual installation

```bash
uv venv .venv
source .venv/bin/activate
uv pip install --python .venv/bin/python -r requirements.txt
npm ci --prefix frontend
npm run build --prefix frontend
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

FastAPI serves the compiled frontend and API from the same address. API explorer:
**http://localhost:8000/docs**.

## Configure providers

Copy the template once, then edit the repository-root `.env`:

```bash
cp .env.example .env
```

Credentials are loaded by the Python backend from `.env`, which is excluded
from Git. Restart FastAPI after changing configuration.

### Sarvam speech recognition

Set `SARVAM_API_KEY` to your Sarvam API key. Use the microphone button on
localhost or HTTPS, or upload an audio file from mission control. Recordings
and uploads feed the selected planner automatically.

### Gemini on Vertex AI

Choose your Google Cloud project and configure Application Default Credentials:

```bash
export WORLDCRAFT_GCP_PROJECT="your-project-id"
gcloud auth login
gcloud config set project "$WORLDCRAFT_GCP_PROJECT"
gcloud services enable aiplatform.googleapis.com --project="$WORLDCRAFT_GCP_PROJECT"
gcloud auth application-default login
gcloud auth application-default set-quota-project "$WORLDCRAFT_GCP_PROJECT"
```

Use a project with billing enabled and grant the calling identity
**Vertex AI User** (`roles/aiplatform.user`) and quota-project
**Service Usage Consumer** (`roles/serviceusage.serviceUsageConsumer`).

Set these values in `.env`:

```dotenv
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=your-model-location
GEMINI_MODEL_ID=your-enabled-gemini-model-id
```

Select a model and endpoint from your project's Vertex AI model catalog. The
backend uses the official Google Gen AI SDK with explicit Vertex AI routing and
ADC. On Google Cloud, ADC can use an attached service account. An externally
managed credential configuration can be selected with `GOOGLE_APPLICATION_CREDENTIALS`.

Provider references: [Google Cloud ADC setup](https://cloud.google.com/vertex-ai/generative-ai/docs/start/gcp-auth)
and [Google Gen AI SDK](https://googleapis.github.io/python-genai/).

### Cyberwave robot workshop

Set `CYBERWAVE_API_KEY` and `CYBERWAVE_ENVIRONMENT_ID` for your environment.
Set `CYBERWAVE_TWIN_ID` to the robot used by the prepared-route bridge.

For a prototype workflow:

1. Build a city and create or import a robot in the workshop.
2. Choose the robot and **Sync city & stage robot** with the simulator stopped.
3. Use **Check physics readiness**, review the displayed cost choice, and start
   a 60-second MuJoCo simulation with the workshop controls.
4. Refresh cloud status, configure the test speed and duration, then run a test.
5. Use **Fetch simulator state**, test history and the 3D view to inspect results.

For the route bridge, dispatch an ambulance, prepare its robot route, sync the
scene and activate the configured Cyberwave Playground before executing it.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `SARVAM_API_KEY` | Sarvam speech-to-text authentication |
| `GOOGLE_CLOUD_PROJECT` | Vertex AI project |
| `GOOGLE_CLOUD_LOCATION` | Vertex AI model endpoint location |
| `GEMINI_MODEL_ID` | Gemini planning model |
| `GOOGLE_APPLICATION_CREDENTIALS` | Optional externally managed ADC configuration path |
| `CYBERWAVE_API_KEY` | Cyberwave authentication |
| `CYBERWAVE_ENVIRONMENT_ID` | Cyberwave environment |
| `CYBERWAVE_TWIN_ID` | Robot twin for the prepared-route bridge |
| `CYBERWAVE_CITY_SCALE` | City-to-robot distance scale; default `0.05` |
| `CYBERWAVE_CITY_ORIGIN_X` | Route-bridge X origin; default `20` |
| `CYBERWAVE_CITY_ORIGIN_Y` | Route-bridge Y origin; default `20` |
| `CYBERWAVE_ROBOT_Z` | Route-bridge robot height; default `0.35` |
| `CYBERWAVE_WORKSHOP_ORIGIN_X` | Workshop X staging origin; default `20` |
| `CYBERWAVE_WORKSHOP_ORIGIN_Y` | Workshop Y staging origin; default `-20` |
| `CYBERWAVE_BASE_URL` | Optional Cyberwave instance URL |

## Development

Activate the project virtual environment, then:

```bash
npm run dev
```

This starts FastAPI on port 8000 and Vite on port 5173. Vite proxies `/api`
requests to FastAPI. Choose another frontend port with:

```bash
npm run dev -- --port 4173
```

## Build and checks

From the activated project environment:

```bash
python -m compileall -q backend scripts tests
python -m pytest -q
npm run build --prefix frontend
python scripts/smoke.py
python scripts/simulation_matrix.py
```

The smoke and simulation-matrix scripts each start and stop a dedicated local
server. Generated reports are saved locally under `docs/`.

Run the provider integration suite with settings exported into your shell:

```bash
RUN_LIVE_INTEGRATIONS=1 python -m pytest -q -m live -rs
```

For the speech test, set `SARVAM_TEST_AUDIO` to a spoken-command audio file.
For the Cyberwave test, activate the configured supported twin in Playground.
The integration suite uses the configured provider accounts.
