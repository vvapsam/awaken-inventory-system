# AWAKEN Inventory & Sales — Web App

A simple, mobile-friendly web app for logging sales and inventory and viewing reports.
Two roles: **Admin** (full control) and **Staff** (log sales/stock, view stock).

Built with FastAPI + PostgreSQL. Designed to run on [Railway](https://railway.app) for ~$5/month.

---

## What it does

**Everyone (staff + admin)**
- Log a sale (one or many items, with payment method)
- Restock, log waste, mark items missing, make adjustments
- View current stock with low-stock alerts

**Admin only**
- Add / edit / deactivate products (SKUs, prices, costs, reorder points)
- Add / edit users, set roles, PINs, and **per-user permissions**
- Reports: sales by day, best sellers, **profit margins**, stock levels
- Export all sales to CSV
- View full history of movements and sales
- A filterable **sales widget** on the dashboard (Today / Yesterday / 7 days /
  30 days / This month) with an animated trend chart, in Manila time.

## Login

Users sign in with a **username + PIN**. The first admin is `admin` (with the PIN
you set in `ADMIN_INITIAL_PIN`). Each user you add gets their own username.

## Roles & permissions

There are two roles:

- **Admin** — full access to everything, including managing users and permissions.
- **Staff** — a normal user whose exact abilities the admin sets on a permission grid.

When you add or edit a staff user, you tick a **Create / Edit / Delete** grid per module:

| Module | Create | Edit | Delete |
|---|---|---|---|
| Sales | log a sale | change payment/note | void a sale |
| Items | add a product | edit a product | remove a product* |
| Receive Inventory | log a restock/return | edit that movement | delete it |
| Adjustment | log waste/missing/adjust | edit that movement | delete it |

Plus a **Reports & access** section: *View reports*, *View stock levels*,
*See costs & profit margins*.

Anything unticked is hidden from that user — the nav link, the page, the buttons,
and the data. Examples: a cashier gets only *Sales → Create* and *View stock levels*;
a stock person gets the whole *Receive Inventory* row plus *View stock levels*; a
supervisor gets *View reports* but not *See costs*, so they see sales but never margins.
Editing and deleting sales/movements happens on the **Records** page, which shows
Edit/Delete buttons only for the permissions a user has. Admins always have everything.

*Deleting an item that already has sales/stock history deactivates it instead of
hard-deleting, so your history stays intact.*

The timezone for "today/yesterday/this month" is set by the `APP_TZ` variable
(default `Asia/Manila`).

---

## Deploy to Railway (step by step)

You'll do this once. It takes about 15 minutes.

### 1. Put the code on GitHub
1. Create a free GitHub account if you don't have one.
2. Create a new **empty** repository (e.g. `awaken-inventory`), set it Private.
3. Upload this whole folder to it (GitHub's web uploader works, or use `git`).

### 2. Create the Railway project + database
1. Sign in at [railway.app](https://railway.app) (you can use your GitHub login).
2. **New Project → Deploy from GitHub repo →** pick `awaken-inventory`.
3. In the project, click **New → Database → Add PostgreSQL**. Railway creates it and
   automatically exposes a `DATABASE_URL` to your app.

### 3. Set two variables
Open your app service → **Variables** tab → add:
- `SECRET_KEY` = a long random string (mash your keyboard, 40+ characters)
- `ADMIN_INITIAL_PIN` = the PIN you want for the first Admin login (e.g. `472913`)

(`DATABASE_URL` is already there from the Postgres plugin — don't touch it.)

### 4. Deploy + open
Railway builds and deploys automatically. When it's live, open the service →
**Settings → Networking → Generate Domain** to get a public URL like
`awaken-inventory.up.railway.app`.

### 5. First login
- Go to your URL, sign in as **Admin** with the `ADMIN_INITIAL_PIN` you set.
- Go to **Staff**, edit Admin to set a new personal PIN.
- Go to **Products** and add your real SKUs.
- Go to **Staff** and add your team (each gets their own PIN).
- Do an initial **Restock** for each product to set your starting stock counts.

That's it — staff can now open the URL on their phones and start logging.

---

## Run it locally (optional, for testing)

```bash
pip install -r requirements.txt
# point at any Postgres you have:
export DATABASE_URL="postgresql://user:pass@localhost:5432/awaken"
export SECRET_KEY="dev"
export ADMIN_INITIAL_PIN="123456"
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

Tables are created automatically on first run.

---

## Branding / logo

The look is minimalist **black / white / teal**. The AWAKEN mark lives at
`app/static/logo.svg` as a crisp vector (used in the header and on the login page,
and as the browser tab icon). It scales to any size and can be re-tinted via CSS.

To swap in a different logo file, replace `app/static/logo.svg` (keep the same
filename), or drop in a PNG and point the `<img>`/`<svg>` references in
`app/templates/base.html` and `app/templates/login.html` at it. Brand colors are
defined once at the top of `app/static/style.css` (`--teal`, `--ink`, etc.) — change
them there and the whole app updates.

## Data model (what's stored)

- **staff** — name, role (admin/staff), hashed PIN, optional phone (for WhatsApp later)
- **products** — SKU, name, category (Food&Beverage / Merchandise), unit (each),
  selling price, cost price, reorder point, active flag
- **stock_movements** — restock / waste / missing / adjustment / return, signed quantity
- **sales** + **sale_items** — one sale can hold many line items

Current stock is *calculated* (restocks − sales − waste/missing), so it never drifts.

---

## What's next (future phases)

- **Claude-chat logging** — staff talk to Claude, it writes to this same database.
- **WhatsApp logging** — staff message a WhatsApp Business number (needs Meta/Twilio
  setup + a small per-message cost). The `phone` field on staff is already here for this.

Both channels reuse this database and logic — no rebuild needed.
