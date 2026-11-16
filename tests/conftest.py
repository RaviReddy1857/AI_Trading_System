"""The test suite reads only the committed settings: config.yaml, never the machine's own config.local.yaml."""
import os

from app.config import NO_LOCAL_ENV

os.environ[NO_LOCAL_ENV] = "1"
