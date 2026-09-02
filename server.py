"""
Jembatan Accurate Online → Claude (MCP server)
Jaya Partindo

Alur:
 1. Buka  https://SERVER/auth   → login Accurate → izinkan → token tersimpan.
 2. Tambahkan  https://SERVER/mcp  di Claude → Settings → Connectors → Add custom connector.
 3. Agent (Davin/Friska) bisa: cek stok, cari barang, lihat piutang, omset per periode, daftar pelanggan.

Env yang wajib:
  ACCURATE_CLIENT_ID, ACCURATE_CLIENT_SECRET   (dari account.accurate.id/developer)
  BASE_URL          contoh https://xxx.up.railway.app  (tanpa / di akhir)
  MCP_TOKEN         kata sandi bebas, dipakai sebagai Bearer token di Claude (opsional tapi disarankan)
Opsional:
  ACCURATE_DB_ID    id database Accurate (kalau punya >1 database). Kosong = pakai yang pertama.
  ACCURATE_REFRESH_TOKEN  isi ini setelah /auth berhasil supaya token tidak hilang saat server restart.
"""
import os, json, time, secrets, urllib.parse
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse

CLIENT_ID = os.environ.get("ACCURATE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ACCURATE_CLIENT_SECRET", "")
if not CLIENT_ID or not CLIENT_SECRET:
    print("PERINGATAN: ACCURATE_CLIENT_ID / ACCURATE_CLIENT_SECRET belum diisi di Variables")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
DB_ID = os.environ.get("ACCURATE_DB_ID", "")
ACC = "https://account.accurate.id"
SCOPES = os.environ.get("ACCURATE_SCOPES", "item_view customer_view sales_invoice_view sales_order_view warehouse_view")
TOK_FILE = "tokens.json"

state = {"access": None, "refresh": os.environ.get("ACCURATE_REFRESH_TOKEN"), "exp": 0, "host": None, "session": None, "sess_exp": 0}
if os.path.exists(TOK_FILE):
    state.update(json.load(open(TOK_FILE)))


def save():
    json.dump({k: state[k] for k in ("access", "refresh", "exp")}, open(TOK_FILE, "w"))


# ---------- OAuth ----------
async def refresh_access():
    if not state["refresh"]:
        raise RuntimeError("Belum login. Buka " + BASE_URL + "/auth di browser dulu.")
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{ACC}/oauth/token", data={"grant_type": "refresh_token", "refresh_token": state["refresh"]},
                         auth=(CLIENT_ID, CLIENT_SECRET))
    r.raise_for_status()
    d = r.json()
    state.update(access=d["access_token"], refresh=d.get("refresh_token", state["refresh"]), exp=time.time() + d.get("expires_in", 3600) - 60)
    save()


async def token():
    if not state["access"] or time.time() > state["exp"]:
        await refresh_access()
    return state["access"]


async def session():
    """Buka database Accurate → dapat host + X-Session-ID (berlaku beberapa jam)."""
    if state["session"] and time.time() < state["sess_exp"]:
        return state["host"], state["session"]
    tok = await token()
    async with httpx.AsyncClient() as c:
        h = {"Authorization": f"Bearer {tok}"}
        dbid = DB_ID
        if not dbid:
            r = await c.get(f"{ACC}/api/db-list.do", headers=h)
            r.raise_for_status()
            dbid = r.json()["d"][0]["id"]
        r = await c.get(f"{ACC}/api/open-db.do", params={"id": dbid}, headers=h)
        r.raise_for_status()
        d = r.json()
    state.update(host=d["host"], session=d["session"], sess_exp=time.time() + 3600 * 5)
    return state["host"], state["session"]


async def api(path: str, params: dict):
    host, sess = await session()
    tok = await token()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{host}/accurate/api/{path}", params=params,
                        headers={"Authorization": f"Bearer {tok}", "X-Session-ID": sess})
    r.raise_for_status()
    d = r.json()
    if not d.get("s", True):
        raise RuntimeError(d.get("d") or d)
    return d


# ---------- MCP tools ----------
TOOLS = {}


def tool(schema):
    """Daftarkan fungsi sebagai MCP tool (tanpa pustaka mcp, supaya tidak tergantung versi)."""
    def deco(fn):
        TOOLS[fn.__name__] = {"fn": fn, "description": fn.__doc__.strip(), "inputSchema": {"type": "object", "properties": schema, "required": [k for k, v in schema.items() if v.get("required")]}}
        for v in schema.values():
            v.pop("required", None)
        return fn
    return deco


S = lambda d, req=False: {"type": "string", "description": d, "required": req}
I = lambda d, req=False: {"type": "integer", "description": d, "required": req}


@tool({"kata_kunci": S("nama/kode barang", True), "halaman": I("halaman, mulai 1")})
async def cari_barang(kata_kunci: str, halaman: int = 1) -> str:
    """Cari barang/sparepart di Accurate berdasarkan nama atau kode. Mengembalikan kode, nama, stok tersedia, dan harga jual."""
    d = await api("item/list.do", {
        "fields": "no,name,availableToSell,unitPrice,itemCategory.name",
        "filter.keywords.op": "CONTAIN", "filter.keywords.val[0]": kata_kunci,
        "sp.page": halaman, "sp.pageSize": 25})
    rows = d.get("d", [])
    if not rows:
        return "Tidak ada barang cocok."
    return "\n".join(f"{r.get('no')} | {r.get('name')} | stok {r.get('availableToSell')} | Rp {r.get('unitPrice')} | {(r.get('itemCategory') or {}).get('name','')}" for r in rows) + f"\n(halaman {halaman}, total {d.get('sp',{}).get('rowCount','?')})"


@tool({"batas": I("batas stok, default 10")})
async def stok_menipis(batas: int = 10) -> str:
    """Daftar barang yang stok tersedianya di bawah batas tertentu (default 10)."""
    d = await api("item/list.do", {
        "fields": "no,name,availableToSell",
        "filter.availableToSell.op": "LESS_THAN", "filter.availableToSell.val[0]": batas,
        "filter.suspended": "false", "sp.pageSize": 100, "sp.sort": "availableToSell|asc"})
    rows = d.get("d", [])
    return "\n".join(f"{r['no']} | {r['name']} | stok {r['availableToSell']}" for r in rows) or "Semua stok di atas batas."


@tool({"kata_kunci": S("nama pelanggan (opsional)"), "halaman": I("halaman")})
async def piutang_pelanggan(kata_kunci: str = "", halaman: int = 1) -> str:
    """Daftar faktur penjualan yang belum lunas (piutang), bisa difilter nama pelanggan."""
    p = {"fields": "number,transDate,dueDate,customer.name,totalAmount,outstanding,age",
         "filter.outstanding.op": "GREATER_THAN", "filter.outstanding.val[0]": 0,
         "sp.page": halaman, "sp.pageSize": 50, "sp.sort": "dueDate|asc"}
    if kata_kunci:
        p.update({"filter.keywords.op": "CONTAIN", "filter.keywords.val[0]": kata_kunci})
    d = await api("sales-invoice/list.do", p)
    rows = d.get("d", [])
    if not rows:
        return "Tidak ada piutang."
    tot = sum(float(r.get("outstanding") or 0) for r in rows)
    return "\n".join(f"{r['number']} | {r.get('transDate')} jatuh tempo {r.get('dueDate')} | {(r.get('customer') or {}).get('name')} | sisa Rp {r.get('outstanding'):,.0f} | umur {r.get('age','?')} hari" for r in rows) + f"\nTotal di halaman ini: Rp {tot:,.0f}"


@tool({"tanggal_awal": S("DD/MM/YYYY", True), "tanggal_akhir": S("DD/MM/YYYY", True), "halaman": I("halaman")})
async def omset(tanggal_awal: str, tanggal_akhir: str, halaman: int = 1) -> str:
    """Rekap faktur penjualan dalam rentang tanggal (format DD/MM/YYYY). Kembalikan daftar faktur & total."""
    d = await api("sales-invoice/list.do", {
        "fields": "number,transDate,customer.name,totalAmount,salesman.name",
        "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": tanggal_awal, "filter.transDate.val[1]": tanggal_akhir,
        "sp.page": halaman, "sp.pageSize": 100})
    rows = d.get("d", [])
    tot = sum(float(r.get("totalAmount") or 0) for r in rows)
    per_sales = {}
    for r in rows:
        s = (r.get("salesman") or {}).get("name") or "-"
        per_sales[s] = per_sales.get(s, 0) + float(r.get("totalAmount") or 0)
    lines = [f"{len(rows)} faktur, total Rp {tot:,.0f} (halaman {halaman} dari {d.get('sp',{}).get('pageCount','?')})", "Per sales: " + ", ".join(f"{k} Rp {v:,.0f}" for k, v in per_sales.items())]
    lines += [f"{r['number']} | {r['transDate']} | {(r.get('customer') or {}).get('name')} | Rp {float(r.get('totalAmount') or 0):,.0f}" for r in rows[:40]]
    return "\n".join(lines)


@tool({"kata_kunci": S("nama toko/bengkel", True)})
async def cari_pelanggan(kata_kunci: str) -> str:
    """Cari toko/bengkel (pelanggan) di Accurate: nama, kota, kontak, sales penanggung jawab."""
    d = await api("customer/list.do", {
        "fields": "customerNo,name,mobilePhone,whatsappNo,billCity,salesman.name",
        "filter.keywords.op": "CONTAIN", "filter.keywords.val[0]": kata_kunci, "sp.pageSize": 25})
    rows = d.get("d", [])
    return "\n".join(f"{r.get('customerNo')} | {r.get('name')} | {r.get('billCity','')} | {r.get('whatsappNo') or r.get('mobilePhone') or ''} | sales {(r.get('salesman') or {}).get('name','-')}" for r in rows) or "Tidak ketemu."


# ---------- FastAPI wrapper ----------
app = FastAPI(title="Accurate MCP - Jaya Partindo")
_nonce = {}


@app.get("/")
def home():
    ok = bool(state["refresh"])
    return HTMLResponse(f"<h3>Accurate MCP Jaya Partindo</h3><p>Status login: {'✅ sudah' if ok else '❌ belum'} — <a href='/auth'>Login Accurate</a></p><p>URL untuk Claude: <code>{BASE_URL}/mcp</code></p>")


@app.get("/auth")
def auth():
    st = secrets.token_urlsafe(16)
    _nonce[st] = True
    q = urllib.parse.urlencode({"client_id": CLIENT_ID, "response_type": "code", "redirect_uri": f"{BASE_URL}/callback", "scope": SCOPES, "state": st})
    return RedirectResponse(f"{ACC}/oauth/authorize?{q}")


@app.get("/callback")
async def callback(request: Request):
    q = dict(request.query_params)
    code = q.get("code")
    if not code:
        return HTMLResponse(
            "<h3>❌ Accurate tidak mengirim kode izin.</h3>"
            "<p>Ini biasanya karena URL OAuth Callback di aplikasi Accurate belum sama persis dengan alamat ini, "
            "atau halaman /callback dibuka langsung tanpa lewat tombol Login.</p>"
            f"<p>Yang dikirim Accurate: <code>{q or 'kosong'}</code></p>"
            f"<p>Pastikan di account.accurate.id/developer, URL OAuth Callback = <code>{BASE_URL}/callback</code>, "
            f"lalu ulangi dari <a href='/auth'>Login Accurate</a>.</p>", status_code=400)
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{ACC}/oauth/token",
                         data={"grant_type": "authorization_code", "code": code, "redirect_uri": f"{BASE_URL}/callback"},
                         auth=(CLIENT_ID, CLIENT_SECRET))
    if r.status_code != 200:
        return HTMLResponse(f"<h3>❌ Gagal tukar token</h3><p>Balasan Accurate:</p><pre>{r.text}</pre>"
                            "<p>Cek ACCURATE_CLIENT_ID / ACCURATE_CLIENT_SECRET di Railway.</p>", status_code=400)
    d = r.json()
    state.update(access=d["access_token"], refresh=d["refresh_token"], exp=time.time() + d.get("expires_in", 3600) - 60)
    save()
    return HTMLResponse(
        "<h3>✅ Accurate tersambung.</h3>"
        "<p>Simpan refresh token ini ke Variables Railway sebagai <code>ACCURATE_REFRESH_TOKEN</code> "
        "supaya tidak perlu login ulang saat server restart:</p>"
        f"<textarea cols=80 rows=3>{d['refresh_token']}</textarea>"
        f"<p>Lalu tambahkan <code>{BASE_URL}/mcp</code> ke Claude → Settings → Connectors.</p>")


@app.middleware("http")
async def guard(request: Request, call_next):
    if MCP_TOKEN and request.url.path.startswith("/mcp"):
        if request.headers.get("authorization") != f"Bearer {MCP_TOKEN}":
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "token salah"}, status_code=401)
    return await call_next(request)


from fastapi.responses import JSONResponse, Response


@app.get("/mcp")
def mcp_get():
    return Response(status_code=405)


@app.delete("/mcp")
def mcp_delete():
    return Response(status_code=200)


@app.post("/mcp")
async def mcp_post(request: Request):
    """MCP Streamable HTTP (stateless, balasan JSON)."""
    body = await request.json()
    msgs = body if isinstance(body, list) else [body]
    out = []
    for m in msgs:
        mid, method, params = m.get("id"), m.get("method", ""), m.get("params") or {}
        if method.startswith("notifications/"):
            continue
        try:
            if method == "initialize":
                res = {"protocolVersion": params.get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "Accurate Jaya Partindo", "version": "1.0"}}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]} for n, t in TOOLS.items()]}
            elif method == "tools/call":
                t = TOOLS.get(params.get("name"))
                if not t:
                    raise ValueError("tool tidak dikenal: " + str(params.get("name")))
                args = {k: v for k, v in (params.get("arguments") or {}).items() if v is not None and v != ""}
                try:
                    text = await t["fn"](**args)
                    res = {"content": [{"type": "text", "text": str(text)}], "isError": False}
                except Exception as e:
                    res = {"content": [{"type": "text", "text": f"Gagal: {e}"}], "isError": True}
            else:
                out.append({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method tidak dikenal: " + method}})
                continue
            out.append({"jsonrpc": "2.0", "id": mid, "result": res})
        except Exception as e:
            out.append({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}})
    if not out:
        return Response(status_code=202)
    return JSONResponse(out[0] if len(out) == 1 and not isinstance(body, list) else out)
