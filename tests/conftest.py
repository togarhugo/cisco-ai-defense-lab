import os

# Same environment for every test module, whatever order pytest imports them in
os.environ.update(AIDEFENSE_MODE="enforce", AIDEFENSE_API_KEY="test", LLM_API_KEY="test",
                  LLM_BASE_URL="http://litellm.test:4000", AIDEFENSE_BASE_URL="https://aid.test",
                  LLM_MODEL="claude-haiku", LLM_MODEL_DIRECT="claude-haiku-direct", AGENT_MODEL="claude-agent",
                  AGENT_MAX_STEPS="8", AIDEFENSE_MCP_MODE="auto")
