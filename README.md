```
     .-.
    (   )     ombott-ng
     |=|      :: the Courier ::
     |=|      one more bottle, now async
    .' '.
   /_____\
```

# ombott-ng

**The Courier.** A fast, async **One More BOTTle** — the HTTP core that carries
requests for [websaw-ng](https://github.com/KellerKev/websaw-ng). Born as a
spin-off of [bottle.py](https://bottlepy.org), it has grown its own async engine
and is now **its own thing**.

![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.7%2B-blue)
![conda](https://img.shields.io/badge/conda-websaw--ng-brightgreen)

## What it adds over bottle

- **ASGI** entrypoint (`app.asgi`) alongside the classic WSGI one — run under
  **uvicorn / hypercorn / granian** with `async def` route handlers.
- **WebSockets** and **Server-Sent Events (SSE)**.
- A fast **radix-tree router** with typed segments and route hooks.
- `before_request` / `after_request` hooks (the seam websaw-ng's metrics + error
  tickets hang off), a rich error-handler system, and streaming responses.

## Install

```bash
pixi project channel add https://prefix.dev/websaw-ng conda-forge
pixi add ombott-ng
# or:  pip install ombott-ng
```

## Quick start

```python
import ombott_ng

app = ombott_ng.Ombott()

@app.get("/")
def home():
    return "hello from ombott-ng"

@app.get("/async")
async def slow():
    return "served over ASGI"

ombott_ng.run(app, server="uvicorn", host="127.0.0.1", port=8080)
```

## Credits & license

**MIT** — see [LICENSE](LICENSE). Derived from **bottle.py** by
**Marcel Hellkamp** (© 2009–2018) and the `ombott` fork by **Valery Kucherov**
(© 2021–2024); async evolution and the `-ng` line © 2026 KellerKev.

---

*Part of the **websaw-ng** platform · forging your dreams.*
