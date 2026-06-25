# Legacy Runtime Removal

The pre-v2 runtime packages were removed during the `feature/full` rebuild.

The active backend implementation lives in:

- `backend/infra`
- `backend/domain`
- `backend/api/admin`
- `backend/runtime`

The public FastAPI entry remains `backend.app:app`. Do not reintroduce parallel legacy runtime packages; migrate any needed behavior into the active v2 layers instead.
