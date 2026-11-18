import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

DEV_JWT_SECRET = "dev-only-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = DATABASE_URL

    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Origins allowed to call the API from a browser (the React dev server by default).
    cors_origins: list[str] = ["http://localhost:5173"]

    # Per-file limit for uploaded resumes (stored in the resumes.content column).
    max_resume_mb: int = 10

    log_dir: str = "logs"
    log_level: str = "INFO"

    # Azure OpenAI: structured field extraction from resume text.
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = "resume-extractor"
    azure_openai_api_version: str = "2024-10-21"

    # Azure Document Intelligence: reads text out of uploaded PDF/DOCX resumes.
    azure_document_intelligence_endpoint: str = ""
    azure_document_intelligence_api_key: str = ""
    azure_document_intelligence_model: str = "prebuilt-layout"

    # Microsoft Foundry: the resume-scorer agent that scores a resume against a job.
    # Signs in with Entra ID (DefaultAzureCredential: `az login` locally, managed identity
    # when deployed); there is no API key.
    azure_ai_project_endpoint: str = ""
    scoring_agent_name: str = "resume-scorer"
    # Empty uses the agent's latest version; pin it so scores stay comparable.
    scoring_agent_version: str = ""
    # Resumes scored at the same time when a batch is requested.
    scoring_max_concurrency: int = 4

settings = Settings()
