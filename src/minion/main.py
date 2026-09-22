"""Process entry point for the Minion API service.

Keeping startup tiny makes application construction testable: importing domain/runtime
modules has no resource side effects, while FastAPI lifespan hooks initialize
persistence, queue consumers and execution sandboxes.
"""
import uvicorn


def main() -> None:
    uvicorn.run("minion.api:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
