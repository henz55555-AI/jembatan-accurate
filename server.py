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
from fastapi.middleware.cors import CORSMiddleware

CLIENT_ID = os.environ.get("ACCURATE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ACCURATE_CLIENT_SECRET", "")
if not CLIENT_ID or not CLIENT_SECRET:
    print("PERINGATAN: ACCURATE_CLIENT_ID / ACCURATE_CLIENT_SECRET belum diisi di Variables")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
DB_ID = os.environ.get("ACCURATE_DB_ID", "")
ACC = "https://account.accurate.id"
SCOPES = os.environ.get("ACCURATE_SCOPES", "item_view customer_view sales_invoice_view sales_order_view warehouse_view vendor_view purchase_invoice_view purchase_order_view sales_receipt_view delivery_order_view sales_return_view item_adjustment_view item_transfer_view employee_view glaccount_view project_view department_view receive_item_view sales_quotation_view")
TOK_FILE = "tokens.json"
REQUIRE_OAUTH = os.environ.get("REQUIRE_OAUTH", "1") != "0"

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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
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


@tool({"jenis": S("jenis data, lihat daftar di keterangan", True), "kata_kunci": S("filter kata kunci (opsional)"),
       "tanggal_awal": S("DD/MM/YYYY (opsional, untuk data transaksi)"), "tanggal_akhir": S("DD/MM/YYYY (opsional)"),
       "halaman": I("halaman, mulai 1"), "urut": S("nama field + |asc atau |desc, mis. transDate|desc")})
async def data_accurate(jenis: str, kata_kunci: str = "", tanggal_awal: str = "", tanggal_akhir: str = "",
                        halaman: int = 1, urut: str = "") -> str:
    """Baca data apa pun dari Accurate Online. Isi 'jenis' dengan salah satu:
    barang, kategori-barang, stok-gudang, gudang, pelanggan, supplier, sales-order, faktur-penjualan,
    pengiriman, retur-penjualan, penerimaan-pembayaran, purchase-order, faktur-pembelian, penerimaan-barang,
    penawaran, karyawan, sales, akun, proyek, departemen, mutasi-barang, penyesuaian-stok.
    Bisa difilter kata_kunci dan rentang tanggal. Gunakan ini untuk data yang tidak dicakup tool lain."""
    peta = {
        "barang": ("item/list.do", "no,name,availableToSell,quantity,unitPrice,vendorPrice,itemCategory.name,itemType"),
        "kategori-barang": ("item-category/list.do", "id,name,parent.name"),
        "stok-gudang": ("item/list-stock.do", "no,name,warehouse.name,quantity,availableToSell"),
        "gudang": ("warehouse/list.do", "id,name,description,street,pic"),
        "pelanggan": ("customer/list.do", "customerNo,name,mobilePhone,whatsappNo,email,billCity,billStreet,customerType.name,salesman.name,balance,creditLimit"),
        "supplier": ("vendor/list.do", "vendorNo,name,mobilePhone,email,billCity,balance"),
        "sales-order": ("sales-order/list.do", "number,transDate,customer.name,totalAmount,statusName,salesman.name"),
        "faktur-penjualan": ("sales-invoice/list.do", "number,transDate,dueDate,customer.name,totalAmount,outstanding,age,salesman.name,statusName"),
        "pengiriman": ("delivery-order/list.do", "number,transDate,customer.name,statusName"),
        "retur-penjualan": ("sales-return/list.do", "number,transDate,customer.name,totalAmount"),
        "penerimaan-pembayaran": ("sales-receipt/list.do", "number,transDate,customer.name,chequeAmount"),
        "purchase-order": ("purchase-order/list.do", "number,transDate,vendor.name,totalAmount,statusName"),
        "faktur-pembelian": ("purchase-invoice/list.do", "number,transDate,dueDate,vendor.name,totalAmount,outstanding"),
        "penerimaan-barang": ("receive-item/list.do", "number,transDate,vendor.name"),
        "penawaran": ("sales-quotation/list.do", "number,transDate,customer.name,totalAmount,statusName"),
        "karyawan": ("employee/list.do", "number,name,email,mobilePhone"),
        "sales": ("salesman/list.do", "salesmanNo,name,email,mobilePhone"),
        "akun": ("glaccount/list.do", "no,name,accountType,balance"),
        "proyek": ("project/list.do", "no,name,customer.name"),
        "departemen": ("department/list.do", "no,name"),
        "mutasi-barang": ("item-transfer/list.do", "number,transDate,fromWarehouse.name,toWarehouse.name"),
        "penyesuaian-stok": ("item-adjustment/list.do", "number,transDate,warehouse.name,description"),
    }
    j = jenis.strip().lower()
    if j not in peta:
        return "Jenis tidak dikenal. Pilihan: " + ", ".join(peta)
    path, fields = peta[j]
    p = {"fields": fields, "sp.page": halaman, "sp.pageSize": 50}
    if kata_kunci:
        p.update({"filter.keywords.op": "CONTAIN", "filter.keywords.val[0]": kata_kunci})
    if tanggal_awal and tanggal_akhir:
        p.update({"filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": tanggal_awal, "filter.transDate.val[1]": tanggal_akhir})
    if urut:
        p["sp.sort"] = urut
    d = await api(path, p)
    rows = d.get("d", [])
    if not rows:
        return f"Tidak ada data {j} yang cocok."

    def datar(o, prefix=""):
        out = {}
        for k, v in (o or {}).items():
            if isinstance(v, dict):
                out.update(datar(v, prefix + k + "."))
            elif not isinstance(v, list):
                out[prefix + k] = v
        return out

    baris = []
    for r in rows:
        f = datar(r)
        baris.append(" | ".join(f"{k}={v}" for k, v in f.items() if v not in (None, "", 0) and k != "id"))
    sp = d.get("sp", {})
    return f"[{j}] {len(rows)} baris (halaman {halaman} dari {sp.get('pageCount','?')}, total {sp.get('rowCount','?')})\n" + "\n".join(baris)


@tool({"kode_atau_nama": S("kode atau nama barang", True)})
async def detail_barang(kode_atau_nama: str) -> str:
    """Detail lengkap satu barang: stok per gudang, harga jual, harga beli, satuan, kategori."""
    d = await api("item/list.do", {
        "fields": "id,no,name,availableToSell,quantity,unitPrice,vendorPrice,unit1Name,itemCategory.name,notes,detailOpenBalance",
        "filter.keywords.op": "CONTAIN", "filter.keywords.val[0]": kode_atau_nama, "sp.pageSize": 5})
    rows = d.get("d", [])
    if not rows:
        return "Barang tidak ditemukan."
    out = []
    for r in rows:
        out.append(f"{r.get('no')} | {r.get('name')} | stok total {r.get('quantity')} | siap jual {r.get('availableToSell')} | jual Rp {r.get('unitPrice')} | beli Rp {r.get('vendorPrice')} | satuan {r.get('unit1Name')} | kategori {(r.get('itemCategory') or {}).get('name','-')}")
        try:
            g = await api("item/list-stock.do", {"fields": "warehouse.name,quantity", "filter.itemId.op": "EQUAL", "filter.itemId.val[0]": r.get("id"), "sp.pageSize": 30})
            for w in g.get("d", []):
                out.append(f"   gudang {(w.get('warehouse') or {}).get('name')}: {w.get('quantity')}")
        except Exception:
            pass
    return "\n".join(out)


@tool({"tanggal_awal": S("DD/MM/YYYY", True), "tanggal_akhir": S("DD/MM/YYYY", True), "top": I("berapa barang teratas, default 15")})
async def barang_terlaris(tanggal_awal: str, tanggal_akhir: str, top: int = 15) -> str:
    """Barang paling laku dalam rentang tanggal, dihitung dari detail faktur penjualan."""
    rekap = {}
    for hal in range(1, 6):
        d = await api("sales-invoice/list.do", {
            "fields": "number,detailItem.item.no,detailItem.item.name,detailItem.quantity,detailItem.totalPrice",
            "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": tanggal_awal, "filter.transDate.val[1]": tanggal_akhir,
            "sp.page": hal, "sp.pageSize": 100})
        rows = d.get("d", [])
        for r in rows:
            for it in (r.get("detailItem") or []):
                item = (it.get("item") or {})
                k = f"{item.get('no','?')} {item.get('name','?')}"
                a = rekap.setdefault(k, [0, 0])
                a[0] += float(it.get("quantity") or 0)
                a[1] += float(it.get("totalPrice") or 0)
        if hal >= (d.get("sp", {}).get("pageCount") or 1):
            break
    if not rekap:
        return "Tidak ada data penjualan pada periode itu (atau detail item tidak tersedia)."
    urut = sorted(rekap.items(), key=lambda x: -x[1][1])[:top]
    return f"Barang terlaris {tanggal_awal} s/d {tanggal_akhir}:\n" + "\n".join(
        f"{i+1}. {k} — {v[0]:,.0f} pcs — Rp {v[1]:,.0f}" for i, (k, v) in enumerate(urut))


# ---------- Penerus AI untuk kantor 3D (biar bisa dibuka di luar Claude) ----------
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


@app.post("/ai")
async def ai_proxy(request: Request):
    """Teruskan permintaan chat agent ke Anthropic memakai API key milik Pak Hendrik."""
    from fastapi.responses import JSONResponse
    if not ANTHROPIC_KEY:
        return JSONResponse({"error": {"message": "ANTHROPIC_API_KEY belum diisi di Railway Variables"}}, status_code=400)
    body = await request.json()
    body.pop("mcp_servers", None)
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post("https://api.anthropic.com/v1/messages", json=body,
                         headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                                  "content-type": "application/json"})
    return JSONResponse(r.json(), status_code=r.status_code)


# ---------- REST sederhana untuk kantor 3D (dipanggil langsung dari browser) ----------
@app.get("/data/{nama}")
async def data_tool(nama: str, request: Request):
    """Panggil tool lewat URL biasa, mis. /data/cari_barang?kata_kunci=CDI
    Dipakai oleh kantor virtual 3D. Balasan: {"hasil": "..."}"""
    from fastapi.responses import JSONResponse
    t = TOOLS.get(nama)
    if not t:
        return JSONResponse({"error": f"tool tidak dikenal: {nama}", "tersedia": list(TOOLS)}, status_code=404)
    args = {k: v for k, v in dict(request.query_params).items() if v not in (None, "")}
    for k, v in list(args.items()):
        if t["inputSchema"]["properties"].get(k, {}).get("type") == "integer":
            try:
                args[k] = int(v)
            except ValueError:
                args.pop(k)
    try:
        return {"hasil": str(await t["fn"](**args))}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/data")
def data_list():
    return {"tools": [{"nama": n, "keterangan": t["description"], "parameter": list(t["inputSchema"]["properties"])} for n, t in TOOLS.items()]}


# ---------- OAuth sederhana untuk Claude (auto-approve) ----------
# Claude mewajibkan connector kustom punya alur OAuth. Server ini menyediakannya
# secara minimal: siapa pun yang buka /authorize langsung disetujui dan diberi
# access token acak. Ini hanya "kunci pintu" ringan, bukan login per pengguna.
oauth = {"clients": {}, "codes": {}, "tokens": set()}


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/api")
@app.get("/.well-known/oauth-protected-resource/mcp")
def prm():
    return {"resource": BASE_URL, "authorization_servers": [BASE_URL]}


@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/openid-configuration")
def asm():
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/authorize",
        "token_endpoint": f"{BASE_URL}/token",
        "registration_endpoint": f"{BASE_URL}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": ["mcp"],
    }


@app.post("/register")
async def register(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    cid = "cl_" + secrets.token_hex(8)
    oauth["clients"][cid] = body or {}
    out = {"client_id": cid, "client_id_issued_at": int(time.time()),
           "redirect_uris": body.get("redirect_uris", []),
           "token_endpoint_auth_method": "none",
           "grant_types": ["authorization_code", "refresh_token"],
           "response_types": ["code"]}
    if body.get("client_name"):
        out["client_name"] = body["client_name"]
    from fastapi.responses import JSONResponse
    return JSONResponse(out, status_code=201)


@app.get("/authorize")
def authorize(request: Request):
    q = dict(request.query_params)
    redirect_uri = q.get("redirect_uri")
    if not redirect_uri:
        raise HTTPException(400, "redirect_uri wajib")
    code = "cd_" + secrets.token_urlsafe(24)
    oauth["codes"][code] = {"exp": time.time() + 600}
    sep = "&" if "?" in redirect_uri else "?"
    url = f"{redirect_uri}{sep}code={code}"
    if q.get("state"):
        url += "&state=" + urllib.parse.quote(q["state"])
    return RedirectResponse(url)


@app.post("/token")
async def token_ep(request: Request):
    raw = (await request.body()).decode() or ""
    form = {}
    ct = request.headers.get("content-type", "")
    if "json" in ct:
        try:
            form = json.loads(raw)
        except Exception:
            form = {}
    if not form and raw:
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
    if not form:
        form = dict(request.query_params)
    gt = form.get("grant_type")
    if gt == "authorization_code":
        c = form.get("code")
        rec = oauth["codes"].pop(c, None)
        if not rec or rec["exp"] < time.time():
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    elif gt != "refresh_token":
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    at = "at_" + secrets.token_urlsafe(32)
    rt = "rt_" + secrets.token_urlsafe(32)
    oauth["tokens"].add(at)
    return {"access_token": at, "token_type": "Bearer", "expires_in": 2592000,
            "refresh_token": rt, "scope": "mcp"}


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.rstrip("/") in ("/mcp", "/api", "/jp"):
        auth = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
        ok = bool(auth) if not MCP_TOKEN else (auth == MCP_TOKEN or auth in oauth["tokens"])
        if not ok:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "invalid_token"}, status_code=401,
                                headers={"WWW-Authenticate": f'Bearer resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource"'})
    return await call_next(request)


from fastapi.responses import JSONResponse, Response


@app.get("/mcp")
@app.get("/mcp/")
@app.get("/api")
@app.get("/jp")
def mcp_get():
    return Response(status_code=405)


@app.delete("/mcp")
@app.delete("/mcp/")
@app.delete("/api")
@app.delete("/jp")
def mcp_delete():
    return Response(status_code=200)


@app.post("/mcp")
@app.post("/mcp/")
@app.post("/api")
@app.post("/api/")
@app.post("/jp")
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
