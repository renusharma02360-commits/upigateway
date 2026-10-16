import os, json, sqlite3, re, time, secrets, uuid, base64, threading, requests
from pathlib import Path
from flask import Flask, request, redirect, session, render_template_string, jsonify, Response
os.environ["OAUTHLIB_INSECURE_TRANSPORT"]="1"
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import qrcode
from PIL import Image

BASE=Path(__file__).parent
load_dotenv(BASE/".env")
app=Flask(__name__)
app.secret_key="final-rozpay-plus-all"
DB=str(BASE/"gateway.db")
CLIENT=str(BASE/"client_secret.json")
SCOPES=["https://www.googleapis.com/auth/gmail.readonly"]
REDIRECT=os.getenv("GOOGLE_REDIRECT_URI","http://127.0.0.1:5000/oauth2callback")

def dbc():
    c=sqlite3.connect(DB, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL;")
    return c

def init_db():
    with dbc() as d:
        d.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE, password TEXT, api_key TEXT, upi_id TEXT, webhook_url TEXT, contact_number TEXT)")
        d.execute("CREATE TABLE IF NOT EXISTS tx (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL, order_id TEXT, custom_order_id TEXT, status TEXT DEFAULT 'PENDING', paid REAL DEFAULT 0, created REAL, screenshot TEXT, user_txn_id TEXT, gmail_txn_id TEXT, gmail_amount REAL, gmail_msg_id TEXT, gmail_date TEXT, customer_email TEXT, offline_note TEXT)")
        d.execute("CREATE TABLE IF NOT EXISTS gmail (user_id INTEGER PRIMARY KEY, email TEXT, cred TEXT)")
        d.execute("CREATE TABLE IF NOT EXISTS used (msg_id TEXT PRIMARY KEY, user_id INTEGER)")
        for col in ["contact_number TEXT","offline_note TEXT"]:
            try: d.execute(f"ALTER TABLE {'users' if 'contact' in col else 'tx'} ADD COLUMN {col}")
            except: pass

init_db()
Path("static").mkdir(exist_ok=True)
Path("static/uploads").mkdir(exist_ok=True)

def page(h):
    return render_template_string("<meta name=viewport content='width=device-width'><style>body{background:#080b18;color:#e2e8f0;font-family:system-ui;padding:10px}.card{background:#111b36;border:1px solid #1e2a4a;border-radius:14px;padding:14px;margin:10px 0}.stat{display:grid;grid-template-columns:1fr 1fr;gap:8px}.statbox{background:#0f172a;border:1px solid #1e293b;padding:12px;border-radius:10px;text-align:center} input,button,select{width:100%;padding:11px;margin:5px 0;border-radius:10px;border:1px solid #1e293b;background:#0f172a;color:#fff} button{background:#6366f1;font-weight:700}.btn-green{background:#16a34a}.btn-red{background:#dc2626}.badge{padding:4px 10px;border-radius:20px;font-size:11px;font-weight:800}.paid{background:#052e16;color:#4ade80}.pending{background:#422006;color:#facc15} code{background:#0f172a;padding:2px 6px;border-radius:6px} table{width:100%;border-collapse:collapse;font-size:13px} th,td{border:1px solid #1e293b;padding:8px} </style><div style='max-width:950px;margin:auto'><div class=card style='display:flex;gap:6px;flex-wrap:wrap'><a href=/dashboard><button>Dashboard</button></a><a href=/transactions><button>Transactions</button></a><a href=/api-docs><button>API Docs</button></a><a href=/settings><button>Settings</button></a><a href=/debug/gmail><button>Debug</button></a><a href=/logout><button class=btn-red>Logout</button></a></div>"+h+"</div>")

def upi_id_for(uid_val=None):
    if uid_val:
        with dbc() as d:
            r=d.execute("SELECT upi_id FROM users WHERE id=?",(uid_val,)).fetchone()
            if r and r[0]: return r[0]
    return os.getenv("MY_UPI_ID","9671246504@fam")
def uid(): return session.get("uid")
def get_creds(user_id):
    with dbc() as d: row=d.execute("SELECT cred FROM gmail WHERE user_id=?",(user_id,)).fetchone()
    if not row: return None
    try:
        info=json.loads(row[0]); c=Credentials.from_authorized_user_info(info, SCOPES)
        if c and c.expired and c.refresh_token: c.refresh(Request()); d2=dbc(); d2.execute("UPDATE gmail SET cred=? WHERE user_id=?",(c.to_json(), user_id)); d2.commit()
        return c
    except: return None
def extract_amt(text):
    m=re.findall(r'(?:RS\.?|INR|\u20b9)\s*([0-9]+\.?[0-9]*)', text.upper())
    try:
        vals=[float(x) for x in m if 0.1<=float(x)<=100000]
        return max(vals) if vals else 0
    except: return 0

# Background auto verifier - offline wale baad me auto PAID
def bg_verifier():
    while True:
        try:
            with dbc() as d: pendings=d.execute("SELECT id,amount,user_id,created FROM tx WHERE status='PENDING' LIMIT 20").fetchall()
            cache={}
            for tid, amt, user_id, cts in pendings:
                if user_id not in cache: cache[user_id]=get_creds(user_id)
                creds=cache[user_id]
                if not creds: continue
                try:
                    svc=build("gmail","v1",credentials=creds, cache_discovery=False)
                    res=svc.users().messages().list(userId='me', q="newer_than:12h", maxResults=20).execute()
                    for mm in res.get("messages",[]):
                        data=svc.users().messages().get(userId='me', id=mm["id"], format='full').execute()
                        its=int(data.get('internalDate','0'))/1000
                        if its < (cts-10): continue
                        g_amt=extract_amt(data.get('snippet',''))
                        if g_amt>0 and abs(g_amt-amt)<1.0:
                            with dbc() as d2:
                                if d2.execute("SELECT msg_id FROM used WHERE msg_id=?",(mm["id"],)).fetchone(): continue
                                d2.execute("INSERT OR IGNORE INTO used VALUES (?,?)",(mm["id"], user_id))
                                d2.execute("UPDATE tx SET status='PAID', paid=?, gmail_amount=?, gmail_date=? WHERE id=?",(g_amt, g_amt, time.strftime("%d %b %I:%M %p", time.localtime(its)), tid))
                            break
                except: pass
            time.sleep(30)
        except: time.sleep(30)
threading.Thread(target=bg_verifier, daemon=True).start()

# --- ALL OLD ROUTES SAME AS BEFORE ---
@app.route("/")
def home():
    if uid(): return redirect("/dashboard")
    return page('<div class=card><h2>PRO UPI Gateway</h2><a href=/register><button>Register</button></a><a href=/login><button>Login</button></a></div>')

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method=="POST":
        e=request.form.get("email","").lower().strip(); p=request.form.get("password","")
        with dbc() as d:
            try: d.execute("INSERT INTO users(email,password,api_key,upi_id) VALUES (?,?,?,?)",(e,generate_password_hash(p), f"live_{secrets.token_hex(12)}", upi_id_for()))
            except: return page("<div class=card>Email exists</div>")
        return redirect("/login")
    return page('<div class=card><h2>Register</h2><form method=post><input name=email required><input name=password type=password required><button>Create</button></form></div>')

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        e=request.form.get("email","").lower().strip(); p=request.form.get("password","")
        with dbc() as d: u=d.execute("SELECT id,password FROM users WHERE email=?",(e,)).fetchone()
        if u and check_password_hash(u[1], p): session.clear(); session["uid"]=u[0]; return redirect("/dashboard")
        return page("<div class=card>Wrong</div>")
    return page('<div class=card><h2>Login</h2><form method=post><input name=email required><input name=password type=password required><button>Login</button></form></div>')

@app.route("/dashboard")
def dashboard():
    if not uid(): return redirect("/login")
    now=time.time(); today_start=now-(now%86400)-19800
    with dbc() as d:
        user=d.execute("SELECT email,api_key,upi_id,webhook_url FROM users WHERE id=?",(uid(),)).fetchone()
        gmail=d.execute("SELECT email FROM gmail WHERE user_id=?",(uid(),)).fetchone()
        today=d.execute("SELECT COALESCE(SUM(paid),0), COUNT(*) FROM tx WHERE user_id=? AND status='PAID' AND created>=?",(uid(),today_start)).fetchone()
        total=d.execute("SELECT COALESCE(SUM(paid),0) FROM tx WHERE user_id=? AND status='PAID'",(uid(),)).fetchone()
        pending=d.execute("SELECT COUNT(*) FROM tx WHERE user_id=? AND status='PENDING'",(uid(),)).fetchone()
        txs=d.execute("SELECT id,amount,status,order_id,created FROM tx WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid(),)).fetchall()
    gcard=f'<div class=card style=border-color:#16a34a><h3>Gmail: {"✅ Connected "+gmail[0] if gmail else "❌ Not Connected - Offline mode chalega (history me PENDING jayega)"}</h3><a href=/debug/gmail><button>Debug Gmail</button></a></div>' if True else ""
    html=gcard+f'<div class=card><div class=stat><div class=statbox>Today<br><b>Rs {today[0]}</b> ({today[1]})</div><div class=statbox>Total<br><b>Rs {total[0]}</b></div><div class=statbox>Pending<br><b>{pending[0]}</b></div><div class=statbox>API<br><code>{user[1][:12]}..</code></div></div></div>'
    html+='<div class=card><form method=post action=/create><input name=amount type=number step=0.01 required placeholder=Amount><button>Create Link</button></form></div>'
    html+='<div class=card><h3>Recent</h3>'
    for tid, amt, st, order, cts in txs:
        dt=time.strftime("%d %b %I:%M%p", time.localtime(cts))
        html+=f'<div style="border:1px solid #1e293b;padding:8px;border-radius:10px;margin:6px 0">#{tid} {order} Rs {amt} <span class=badge {("paid" if st=="PAID" else "pending")}>{st}</span> {dt} <a href=/pay/{tid}>Open Pay</a></div>'
    html+='</div>'
    return page(html)

@app.route("/transactions")
def transactions():
    if not uid(): return redirect("/login")
    q=request.args.get("q","")
    with dbc() as d:
        if q: txs=d.execute("SELECT id,amount,status,order_id,created FROM tx WHERE user_id=? AND order_id LIKE? ORDER BY id DESC LIMIT 100",(uid(), f"%{q}%")).fetchall()
        else: txs=d.execute("SELECT id,amount,status,order_id,created FROM tx WHERE user_id=? ORDER BY id DESC LIMIT 100",(uid(),)).fetchall()
    html='<div class=card><form><input name=q placeholder=Search Order value="'+q+'"><button>Search</button></form><a href=/export_csv><button>Export CSV</button></a></div><div class=card><table><tr><th>ID</th><th>Order</th><th>Amt</th><th>Status</th><th>Action</th></tr>'
    for tid, amt, st, order, cts in txs:
        html+=f'<tr><td>{tid}</td><td>{order}</td><td>{amt}</td><td>{st}</td><td><a href=/pay/{tid}>Pay</a> | <a href=/manual_paid/{tid}>Mark Paid</a> | <a href=/delete_tx/{tid}>Del</a></td></tr>'
    return page(html+'</table></div>')

@app.route("/export_csv")
def export_csv():
    if not uid(): return redirect("/login")
    with dbc() as d: txs=d.execute("SELECT id,order_id,amount,paid,status,created FROM tx WHERE user_id=?",(uid(),)).fetchall()
    import io, csv
    si=io.StringIO(); w=csv.writer(si); w.writerow(["ID","Order","Amount","Paid","Status","Date"])
    for t in txs: w.writerow([t[0],t[1],t[2],t[3],t[4],time.strftime("%Y-%m-%d %H:%M", time.localtime(t[5]))])
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition":"attachment;filename=tx.csv"})

@app.route("/settings", methods=["GET","POST"])
def settings():
    if not uid(): return redirect("/login")
    with dbc() as d: user=d.execute("SELECT upi_id,webhook_url,contact_number FROM users WHERE id=?",(uid(),)).fetchone()
    if request.method=="POST":
        upi=request.form.get("upi_id","").strip(); hook=request.form.get("webhook_url","").strip(); contact=request.form.get("contact_number","").strip()
        with dbc() as d: d.execute("UPDATE users SET upi_id=?, webhook_url=?, contact_number=? WHERE id=?",(upi, hook, contact, uid()))
        return redirect("/settings")
    return page(f'<div class=card><h3>Settings</h3><form method=post><p>UPI ID:</p><input name=upi_id value="{user[0] or ""}"><p>Webhook URL:</p><input name=webhook_url value="{user[1] or ""}"><p>Contact Number (Gmail Offline me dikhega):</p><input name=contact_number value="{user[2] or ""}" placeholder="9876543210"><button class=btn-green>Save</button></form></div>')

@app.route("/create", methods=["POST"])
def create():
    if not uid(): return redirect("/login")
    amt=float(request.form.get("amount",1)); order="ORD_"+uuid.uuid4().hex[:5].upper(); ts=time.time()
    with dbc() as d: cur=d.execute("INSERT INTO tx(user_id,amount,order_id,created) VALUES (?,?,?,?)",(uid(),amt,order,ts)); tid=cur.lastrowid
    qrcode.make(f"upi://pay?pa={upi_id_for(uid())}&am={amt:.2f}&cu=INR&tn={order}").save(f"static/qr_{tid}.png")
    return redirect(f"/pay/{tid}")

@app.route("/manual_paid/<int:tid>")
def manual_paid(tid):
    if not uid(): return redirect("/login")
    with dbc() as d: d.execute("UPDATE tx SET status='PAID', paid=(SELECT amount FROM tx WHERE id=?), gmail_amount=(SELECT amount FROM tx WHERE id=?) WHERE id=? AND user_id=?",(tid, tid, tid, uid()))
    return redirect("/dashboard")

@app.route("/delete_tx/<int:tid>")
def delete_tx(tid):
    if not uid(): return redirect("/login")
    with dbc() as d: d.execute("DELETE FROM tx WHERE id=? AND user_id=?",(tid, uid()))
    return redirect("/dashboard")

@app.route("/api-docs")
def api_docs():
    if not uid(): return redirect("/login")
    with dbc() as d: user=d.execute("SELECT api_key FROM users WHERE id=?",(uid(),)).fetchone()
    key=user[0]; base=request.host_url.rstrip("/")
    return page(f'<div class=card><h2>API Docs</h2><p>Base: <code>{base}</code></p><p>Key: <code>{key}</code></p><p>Create: <code>{base}/api/v1/create-order?api_key={key}&amount=100</code></p><p>Status: <code>{base}/api/v1/status/ID?api_key={key}</code></p><p>All: <code>{base}/api/v1/transactions?api_key={key}</code></p></div>')

@app.route("/api/v1/create-order", methods=["GET","POST"])
def api_create():
    key=request.args.get("api_key") or request.headers.get("X-API-KEY") or (request.json.get("api_key") if request.is_json else None)
    if not key: return jsonify(error="api_key required"),401
    with dbc() as d: user=d.execute("SELECT id FROM users WHERE api_key=?",(key,)).fetchone()
    if not user: return jsonify(error="invalid key"),401
    amt=float((request.json.get("amount") if request.is_json else None) or request.args.get("amount") or 1)
    order="ORD_"+uuid.uuid4().hex[:6].upper(); ts=time.time()
    with dbc() as d: cur=d.execute("INSERT INTO tx(user_id,amount,order_id,created) VALUES (?,?,?,?)",(user[0],amt,order,ts)); tid=cur.lastrowid
    link=f"upi://pay?pa={upi_id_for(user[0])}&am={amt:.2f}&cu=INR&tn={order}"
    qrcode.make(link).save(f"static/qr_{tid}.png")
    base=request.host_url.rstrip("/")
    return jsonify(transaction_id=tid, order_id=order, amount=amt, payment_url=f"{base}/pay/{tid}", status="PENDING", upi_link=link)

@app.route("/api/v1/status/<int:tid>")
def api_status(tid):
    key=request.args.get("api_key") or request.headers.get("X-API-KEY")
    with dbc() as d:
        if key:
            u=d.execute("SELECT id FROM users WHERE api_key=?",(key,)).fetchone()
            r=d.execute("SELECT amount,status,order_id,paid,gmail_txn_id FROM tx WHERE id=? AND user_id=?",(tid, u[0])).fetchone() if u else None
        else:
            r=d.execute("SELECT amount,status,order_id,paid,gmail_txn_id FROM tx WHERE id=?",(tid,)).fetchone()
    if not r: return jsonify(error="not found"),404
    return jsonify(transaction_id=tid, amount=r[0], status=r[1], order_id=r[2], paid=r[3], upi_txn_id=r[4])

@app.route("/api/v1/transactions")
def api_transactions():
    key=request.args.get("api_key") or request.headers.get("X-API-KEY")
    if not key: return jsonify(error="api_key required"),401
    with dbc() as d: user=d.execute("SELECT id FROM users WHERE api_key=?",(key,)).fetchone()
    if not user: return jsonify(error="invalid key"),401
    with dbc() as d: txs=d.execute("SELECT id,order_id,amount,paid,status FROM tx WHERE user_id=? ORDER BY id DESC LIMIT 50",(user[0],)).fetchall()
    return jsonify(transactions=[{"id":t[0],"order_id":t[1],"amount":t[2],"paid":t[3],"status":t[4]} for t in txs])

# --- PAY PAGE ONLY CHANGED TO ROZ PAY STYLE + OFFLINE ---
@app.route("/pay/<int:tid>")
def pay_page(tid):
    with dbc() as d: r=d.execute("SELECT amount,status,order_id,created,gmail_amount,gmail_txn_id,gmail_date,user_id FROM tx WHERE id=?",(tid,)).fetchone()
    if not r: return page("Invalid")
    amt,st,order,c_ts,g_amt,g_txn,g_date,user_id = r
    with dbc() as d: uinfo=d.execute("SELECT contact_number, upi_id FROM users WHERE id=?",(user_id,)).fetchone()
    contact=uinfo[0] if uinfo and uinfo[0] else "Contact Owner"
    upi=uinfo[1] if uinfo and uinfo[1] else upi_id_for(user_id)
    if st=="PAID":
        dt=g_date or time.strftime("%d %B %Y, %I:%M %p", time.localtime(c_ts))
        return page(f'<div class=card style="text-align:center;border:2px solid #16a34a;background:#fff;color:#111"><h1 style="font-size:70px">✅</h1><h1 style="color:#166534">Payment Successful</h1><h2>Rs {g_amt or amt}</h2><p>Order: {order}</p><p>Date: {dt}</p><p>Txn: {g_txn or "Verified"}</p><p style="background:#dcfce7;padding:10px;border-radius:10px">Gmail se verified on {dt}</p></div>')

    return page(f'''
<div style="max-width:480px;margin:auto;background:#f8fafc;min-height:100vh;padding:10px">
<div style="background:#111b36;color:#fff;padding:12px;border-radius:16px;text-align:center"><h2>Secure Payment</h2><p style="font-size:12px;opacity:0.8">Roz Pay / Cash Pay Style</p></div>

<div class=card id="qrBox" style="text-align:center;background:#fff;color:#111">
<p style="color:#64748b">Order ID</p><h3>{order}</h3>
<h1 style="font-size:42px;margin:10px 0">₹ {amt:.2f}</h1>
<p id="timer" style="color:#dc2626;font-weight:700">10:00 remaining</p>
<img src=/static/qr_{tid}.png style="width:260px;background:#fff;padding:12px;border-radius:16px;display:block;margin:10px auto;border:2px solid #e2e8f0">
<p>UPI ID: <code id="upiTxt" style="background:#f1f5f9;color:#111">{upi}</code> <button onclick="navigator.clipboard.writeText('{upi}')" style="width:auto;padding:4px 10px;background:#111;color:#fff">Copy</button></p>
<div style="display:flex;gap:6px;justify-content:center"><span style="background:#f1f5f9;padding:6px 12px;border-radius:20px;font-size:12px">PhonePe</span><span style="background:#f1f5f9;padding:6px 12px;border-radius:20px;font-size:12px">GPay</span><span style="background:#f1f5f9;padding:6px 12px;border-radius:20px;font-size:12px">Paytm</span><span style="background:#f1f5f9;padding:6px 12px;border-radius:20px;font-size:12px">FamPay</span></div>

<div id="statusBox" style="margin-top:15px;background:#f8fafc;padding:12px;border-radius:12px;border:1px dashed #cbd5e1;color:#111">
<p>Status: <b id="s">{st}</b></p>
<p id="debugInfo" style="font-size:11px;color:#64748b">Debug: Waiting for Gmail Rs {amt}...</p>
<div id="box">Checking Gmail for same amount...</div>
</div>

<div id="offlineBox" style="display:none;margin-top:12px;background:#fef3c7;padding:12px;border-radius:12px;border:1px solid #facc15;color:#111">
<p style="color:#92400e;font-weight:700">⚠️ Gmail Offline / Net Issue</p>
<p style="font-size:13px">Gmail verify nahi ho pa raha. Customer ne pay kiya hai to:</p>
<p style="margin-top:8px">Contact Owner: <b>{contact}</b></p>
<p style="font-size:11px">Payment PENDING me history me save ho gaya hai, owner baad me manual PAID kar dega. History me count hota rahega.</p>
<button onclick="document.getElementById('manualForm').style.display='block'" style="background:#111;color:#fff">I have paid</button>
</div>

<div id="manualForm" style="display:none;margin-top:10px">
<form method=post action=/submit_proof/{tid} enctype=multipart/form-data>
<input type=file name=screenshot accept=image/* style="background:#fff;color:#111">
<input type=text name=utr placeholder="UTR / Txn ID" style="background:#fff;color:#111">
<button class=btn-green>Submit Proof</button>
</form>
</div>

<button id="btn" onclick="checkNow()" style="margin-top:10px;background:#6366f1;color:#fff">Check Payment Status</button>
</div>

<div class=card id="successBox" style="display:none;text-align:center;border:2px solid #16a34a;background:#fff;color:#111">
<h1 style="font-size:70px">✅</h1><h1 style="color:#166534">Payment Successful</h1><h2 id="paidAmt">Rs {amt}</h2><p id="paidOrder">Order: {order}</p><p id="paidDate"></p><p id="paidTxn"></p><p style="background:#dcfce7;padding:10px;border-radius:10px">Scanner auto hide - Gmail verified</p>
</div>
</div>

<script>
let timeLeft=600;
setInterval(()=>{{ timeLeft--; let m=Math.floor(timeLeft/60), s=timeLeft%60; let t=document.getElementById("timer"); if(t) t.innerText = (m<10?"0"+m:m)+":"+(s<10?"0"+s:s)+" remaining"; }},1000);
function checkNow(){{
 let b=document.getElementById("btn"); b.disabled=true; b.innerText="Checking Gmail...";
 fetch("/api/check/{tid}").then(r=>r.json()).then(d=>{{
   document.getElementById("s").innerText=d.status;
   document.getElementById("debugInfo").innerText = (d.debug||"")+" | "+new Date().toLocaleTimeString();
   document.getElementById("box").innerHTML = d.msg;
   b.disabled=false; b.innerText="Check Payment Status";
   if(d.status=="OFFLINE" || d.status=="NO_GMAIL" || d.status=="PENDING_OFFLINE"){{ document.getElementById("offlineBox").style.display="block"; }}
   if(d.status=="PAID"){{
     document.getElementById("qrBox").style.display="none";
     document.getElementById("successBox").style.display="block";
     document.getElementById("paidAmt").innerText="Rs "+(d.gmail_amount||"{amt}");
     document.getElementById("paidDate").innerText="Date: "+d.gmail_date;
     document.getElementById("paidTxn").innerText="Txn: "+(d.gmail_txn||"Verified");
     setTimeout(()=>location.reload(),1200);
   }}
 }})
}}
setInterval(checkNow,5000); checkNow();
</script>
''')

@app.route("/submit_proof/<int:tid>", methods=["POST"])
def submit_proof(tid):
    f=request.files.get("screenshot"); utr=request.form.get("utr","").strip()
    fname=""
    if f and f.filename:
        fname=f"uploads/{tid}_{secure_filename(f.filename)}"; f.save("static/"+fname)
        try: im=Image.open("static/"+fname); im.thumbnail((800,800)); im.save("static/"+fname, optimize=True, quality=70)
        except: pass
    with dbc() as d: d.execute("UPDATE tx SET screenshot=?, user_txn_id=?, offline_note=? WHERE id=?",(fname, utr, "User submitted proof - offline mode", tid))
    return redirect(f"/pay/{tid}")

@app.route("/api/check/<int:tid>")
def api_check(tid):
    with dbc() as d: r=d.execute("SELECT amount,user_id,created FROM tx WHERE id=?",(tid,)).fetchone()
    if not r: return jsonify(status="INVALID", msg="Not found", debug="", gmail_amount=0, gmail_date="", gmail_txn="")
    amt,u_id,c_ts = r
    creds=get_creds(u_id)
    if not creds:
        return jsonify(status="OFFLINE", msg="⚠️ Gmail Offline<br>Payment PENDING me save hai. Owner ko contact karo.<br>History me count ho raha hai.", debug="Gmail offline - PENDING saved", gmail_amount=0, gmail_date="", gmail_txn="")
    try:
        svc=build("gmail","v1",credentials=creds, cache_discovery=False)
        with dbc() as d: used=[x[0] for x in d.execute("SELECT msg_id FROM used WHERE user_id=?",(u_id,)).fetchall()]
        res=svc.users().messages().list(userId='me', q="newer_than:12h", maxResults=20).execute()
        msgs=res.get("messages",[])
        debug_text=f"History me {len(msgs)} mails (12h) checked"
        for mm in msgs:
            if mm["id"] in used: continue
            data=svc.users().messages().get(userId='me', id=mm["id"], format='full').execute()
            its=int(data.get('internalDate','0'))/1000
            if its < (c_ts-10): continue
            snippet=data.get('snippet','')
            g_amt=extract_amt(snippet)
            gmail_date_str=time.strftime("%d %B %Y %I:%M %p", time.localtime(its))
            debug_text=f"Last Gmail: Rs {g_amt} on {gmail_date_str}"
            if g_amt>0 and abs(g_amt-amt)<1.0:
                with dbc() as d:
                    d.execute("INSERT OR IGNORE INTO used VALUES (?,?)",(mm["id"], u_id))
                    d.execute("UPDATE tx SET status='PAID', paid=?, gmail_amount=?, gmail_txn_id=?, gmail_date=? WHERE id=?",(g_amt, g_amt, "", gmail_date_str, tid))
                return jsonify(status="PAID", msg=f"✅ Payment Received Rs {g_amt}<br>Date: {gmail_date_str}<br>Scanner auto hide ho gaya", debug=debug_text, gmail_amount=g_amt, gmail_txn="", gmail_date=gmail_date_str)
        return jsonify(status="PENDING", msg=f"⏳ Waiting for Rs {amt}... Scanner se pay karo", debug=debug_text, gmail_amount=0, gmail_date="", gmail_txn="")
    except Exception as e:
        return jsonify(status="PENDING_OFFLINE", msg=f"⚠️ Gmail temp offline, payment PENDING me save hai.<br>History me count ho raha hai.<br>{str(e)[:100]}", debug="Temp offline - PENDING saved", gmail_amount=0, gmail_date="", gmail_txn="")

@app.route("/debug/gmail")
def debug_gmail():
    if not uid(): return redirect("/login")
    creds=get_creds(uid())
    if not creds: 
        return page('<div class=card><h3>❌ Gmail Not Connected</h3><p>Offline mode: Payment PENDING me jayega, history me count hoga. Connect karne pe auto PAID.</p><a href=/connect><button>Connect Gmail</button></a></div>')
    try:
        svc=build("gmail","v1",credentials=creds, cache_discovery=False)
        res=svc.users().messages().list(userId='me', q="newer_than:12h", maxResults=15).execute()
        html='<div class=card><h3>Gmail History - Last 12 Hours (Counted)</h3><p>Total mails: '+str(len(res.get("messages",[])))+'</p></div>'
        for mm in res.get("messages",[])[:10]:
            try:
                data=svc.users().messages().get(userId='me', id=mm["id"], format='metadata', metadataHeaders=['Subject','From','Date']).execute()
                snippet=data.get('snippet','')
                its=int(data.get('internalDate','0'))/1000
                import time as _t
                dt=_t.strftime("%d %b %I:%M %p", _t.localtime(its))
                amt=extract_amt(snippet)
                subj=""
                for h in data.get('payload',{}).get('headers',[]):
                    if h['name'].lower()=='subject': subj=h['value']
                html+=f'<div class=card><p><b>{dt}</b> - Rs {amt}</p><p><b>Sub:</b> {subj[:80]}</p><p>{snippet[:180]}</p></div>'
            except Exception as e_inner:
                html+=f'<div class=card><p>Mail ID {mm["id"][:10]} read error: {str(e_inner)[:100]}</p></div>'
                continue
        return page(html+'<a href=/dashboard><button>Back to Dashboard</button></a>')
    except Exception as e:
        import traceback
        traceback.print_exc()
        return page(f'<div class=card><h3 style=color:#f87171>Debug Error</h3><p>{str(e)[:300]}</p><p>Gmail token expire ho gaya ho to Disconnect karke dubara Connect karo</p><a href=/disconnect_gmail><button class=btn-red>Disconnect & Reconnect</button></a><a href=/dashboard><button>Back</button></a></div>')

@app.route("/connect")
def conn():
    if not uid(): return redirect("/login")
    flow=Flow.from_client_secrets_file(CLIENT, scopes=SCOPES, redirect_uri=REDIRECT)
    url,state=flow.authorization_url(access_type="offline", prompt="consent")
    Path("v.txt").write_text(flow.code_verifier); Path("st.txt").write_text(state)
    return redirect(url)

@app.route("/oauth2callback")
def cb():
    if not uid(): return redirect("/login")
    flow=Flow.from_client_secrets_file(CLIENT, scopes=SCOPES, state=Path("st.txt").read_text(), redirect_uri=REDIRECT)
    flow.code_verifier=Path("v.txt").read_text(); flow.fetch_token(authorization_response=request.url)
    svc=build("gmail","v1",credentials=flow.credentials)
    email=svc.users().getProfile(userId="me").execute()["emailAddress"]
    with dbc() as d: d.execute("INSERT OR REPLACE INTO gmail VALUES (?,?,?)",(uid(), email, flow.credentials.to_json()))
    return redirect("/dashboard")

@app.route("/disconnect_gmail")
def dis_gmail():
    if not uid(): return redirect("/login")
    with dbc() as d: d.execute("DELETE FROM gmail WHERE user_id=?",(uid(),))
    return redirect("/dashboard")

@app.route("/regen_api")
def regen_api():
    if not uid(): return redirect("/login")
    new=f"live_{secrets.token_hex(12)}"
    with dbc() as d: d.execute("UPDATE users SET api_key=? WHERE id=?",(new, uid()))
    return redirect("/dashboard")

@app.route("/logout")
def logout(): session.clear(); return redirect("/")

if __name__=="__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
