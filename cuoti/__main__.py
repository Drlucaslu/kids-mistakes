"""python -m cuoti  →  serve on $PORT (8080)."""
import logging

import uvicorn

from .config import get_settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run("cuoti.web:app", host="0.0.0.0", port=get_settings().port, proxy_headers=True,
                forwarded_allow_ips="*")
