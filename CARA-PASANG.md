# Integrasi Accurate Online → Claude (agent Davin & Friska)

Total ±30 menit. Tidak mengubah apa pun di Accurate — hanya **baca** data (stok, piutang, omset, pelanggan).

## 1. Daftar aplikasi di Accurate (5 menit)
1. Buka https://account.accurate.id/developer → login pakai akun Accurate Bapak (harus akun **pemilik/admin**).
2. *Create App* → nama: `Claude Jaya Partindo`.
3. Redirect URI isi: `https://ALAMAT-SERVER/callback` (alamat server dari langkah 2, boleh diisi belakangan lalu diedit).
4. Scope centang: Item, Customer, Sales Invoice, Sales Order, Receipt, Warehouse (semua *view*).
5. Simpan **Client ID** dan **Client Secret**.

## 2. Deploy server (10 menit)
Pakai Railway (https://railway.app), gratis/murah:
1. New Project → Deploy from GitHub/upload folder ini.
2. Variables:
   - `ACCURATE_CLIENT_ID` = …
   - `ACCURATE_CLIENT_SECRET` = …
   - `BASE_URL` = `https://xxx.up.railway.app` (dari Settings → Generate Domain)
   - `MCP_TOKEN` = kata sandi bebas, mis. `jp-4milyar`
3. Kembali ke account.accurate.id/developer, pastikan Redirect URI = `BASE_URL/callback`.

## 3. Login sekali (2 menit)
Buka `BASE_URL/auth` di browser → login Accurate → *Allow*.
Halaman akan menampilkan **refresh token** → tempel ke Variables Railway `ACCURATE_REFRESH_TOKEN` (supaya tidak perlu login ulang saat server restart).
Kalau punya lebih dari 1 database Accurate, isi juga `ACCURATE_DB_ID`.

## 4. Sambungkan ke Claude (2 menit)
Claude → Settings → Connectors → **Add custom connector**
- Name: Accurate Jaya Partindo
- URL: `BASE_URL/mcp`
- Advanced → Bearer token / OAuth client secret: isi `MCP_TOKEN`.

## 5. Coba
Di Claude: "Davin, cek stok CDI TRQ Beat" / "Friska, piutang yang lewat jatuh tempo" / "omset 01/09/2026 sampai 30/09/2026 per sales".

Tool yang tersedia: `cari_barang`, `stok_menipis`, `piutang_pelanggan`, `omset`, `cari_pelanggan`.

## Kalau error
- `Belum login` → ulangi langkah 3.
- `401` → token di Claude tidak sama dengan `MCP_TOKEN`.
- Field kosong/`null` → nama field Accurate bisa beda tipis per versi; kirim pesan errornya ke Claude, tinggal disesuaikan.
