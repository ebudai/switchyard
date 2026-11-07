"""Writes the local-Qwen profile config Hermes ran with in the SYRD-734 probe (render_config, as a role gets it)."""
import json, sys
sys.path.insert(0, sys.argv[1])
from scripts.hermes_local_profile import LocalProfile, render_config
p = LocalProfile(model="Qwen/Qwen3.8-27B", base_url="http://127.0.0.1:18734/v1", server="llama.cpp", revision="r" * 40,
                 artifact="a", quantization="q4", context=65536)
print(json.dumps(render_config(None, p, fresh_per_ticket=True)))
