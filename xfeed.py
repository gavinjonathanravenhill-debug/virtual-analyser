"""X news feed: a slide-out panel injected into every HTML page.

Two modes:
  * X_BEARER_TOKEN set  -> /api/xfeed pulls recent posts from the X API v2
                           (query from X_FEED_QUERY), cached to save API reads.
  * no token            -> the panel shows embedded X timelines for the
                           accounts in X_FEED_ACCOUNTS (free, no API key).
"""
import os
import time
import threading

import requests
from flask import Blueprint, jsonify

xfeed_bp = Blueprint("xfeed", __name__)

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "").strip()
X_FEED_ACCOUNTS = [a.strip().lstrip("@") for a in os.environ.get(
    "X_FEED_ACCOUNTS", "WatcherGuru,lookonchain,whale_alert,tier10k,solana"
).split(",") if a.strip()]
X_FEED_QUERY = os.environ.get(
    "X_FEED_QUERY",
    "(" + " OR ".join(f"from:{a}" for a in X_FEED_ACCOUNTS) + ") -is:retweet",
)
CACHE_SECONDS = int(os.environ.get("X_FEED_CACHE_SECONDS", "120"))

_cache = {"ts": 0, "data": None}
_lock = threading.Lock()


def _fetch_api():
    r = requests.get(
        "https://api.x.com/2/tweets/search/recent",
        headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
        params={
            "query": X_FEED_QUERY,
            "max_results": 30,
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "username,name,profile_image_url,verified",
        },
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    users = {u["id"]: u for u in body.get("includes", {}).get("users", [])}
    posts = []
    for t in body.get("data", []):
        u = users.get(t.get("author_id"), {})
        m = t.get("public_metrics", {})
        posts.append({
            "id": t["id"],
            "text": t.get("text", ""),
            "created_at": t.get("created_at"),
            "username": u.get("username", ""),
            "name": u.get("name", ""),
            "avatar": u.get("profile_image_url", ""),
            "likes": m.get("like_count", 0),
            "reposts": m.get("retweet_count", 0),
            "url": f"https://x.com/{u.get('username', 'i')}/status/{t['id']}",
        })
    return posts


@xfeed_bp.route("/api/xfeed")
def xfeed_api():
    if not X_BEARER_TOKEN:
        return jsonify({"mode": "embed", "accounts": X_FEED_ACCOUNTS})
    with _lock:
        fresh = _cache["data"] is not None and time.time() - _cache["ts"] < CACHE_SECONDS
        if not fresh:
            try:
                _cache["data"] = _fetch_api()
                _cache["ts"] = time.time()
                _cache.pop("error", None)
            except Exception as e:  # keep serving the last good copy
                _cache["error"] = str(e)[:200]
        return jsonify({
            "mode": "api",
            "accounts": X_FEED_ACCOUNTS,
            "posts": _cache["data"] or [],
            "updated": _cache["ts"],
            "error": _cache.get("error"),
        })


XFEED_WIDGET = r"""
<!-- X news feed (injected by xfeed.py) -->
<style>
#xf-tab{position:fixed;right:0;top:50%;transform:translateY(-50%);z-index:9998;background:#0d1120;color:#e8ecff;border:1px solid #1a2040;border-right:none;border-radius:6px 0 0 6px;padding:12px 7px;cursor:pointer;font:700 11px 'Space Mono',monospace;letter-spacing:2px;writing-mode:vertical-rl}
#xf-tab:hover{color:#7fff6e}
#xf-panel{position:fixed;top:0;right:0;height:100vh;width:360px;max-width:100vw;z-index:9999;background:#060810;border-left:1px solid #1a2040;transform:translateX(100%);transition:transform .25s ease;display:flex;flex-direction:column;font-family:'Space Mono',monospace;color:#e8ecff;box-shadow:-8px 0 24px rgba(0,0,0,.5)}
#xf-panel.open{transform:none}
#xf-head{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid #1a2040;font-size:11px;letter-spacing:3px}
#xf-head button{background:none;border:none;color:#5a6480;font-size:18px;cursor:pointer}
#xf-tabs{display:flex;flex-wrap:wrap;gap:4px;padding:8px 10px;border-bottom:1px solid #1a2040}
#xf-tabs button{background:#0d1120;border:1px solid #1a2040;color:#5a6480;font:10px 'Space Mono',monospace;padding:4px 8px;border-radius:4px;cursor:pointer}
#xf-tabs button.on{color:#7fff6e;border-color:rgba(127,255,110,.4)}
#xf-body{flex:1;overflow-y:auto;padding:8px 10px}
.xf-post{display:block;text-decoration:none;color:inherit;border:1px solid #1a2040;background:#0d1120;border-radius:6px;padding:10px;margin-bottom:8px}
.xf-post:hover{border-color:#6eb4ff}
.xf-who{display:flex;align-items:center;gap:8px;font-size:11px;margin-bottom:6px}
.xf-who img{width:22px;height:22px;border-radius:50%}
.xf-who b{color:#e8ecff}.xf-who span{color:#5a6480}
.xf-text{font-size:12px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word;font-family:system-ui,sans-serif}
.xf-meta{font-size:10px;color:#5a6480;margin-top:6px}
.xf-note{font-size:10px;color:#5a6480;padding:6px 2px}
@media(max-width:600px){#xf-panel{width:100vw}}
</style>
<div id="xf-tab" title="X news feed">𝕏 NEWS</div>
<aside id="xf-panel" aria-label="X news feed">
  <div id="xf-head"><span>𝕏 NEWS FEED</span><button id="xf-close" aria-label="Close">×</button></div>
  <div id="xf-tabs"></div>
  <div id="xf-body"><div class="xf-note">Loading…</div></div>
</aside>
<script>
(function(){
  var panel=document.getElementById('xf-panel'),body=document.getElementById('xf-body'),
      tabs=document.getElementById('xf-tabs'),cfg=null,filter='ALL',timer=null,widgets=false;
  function esc(s){return String(s||'').replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
  function ago(iso){var s=(Date.now()-new Date(iso))/1000;if(s<60)return Math.floor(s)+'s';if(s<3600)return Math.floor(s/60)+'m';if(s<86400)return Math.floor(s/3600)+'h';return Math.floor(s/86400)+'d'}
  function drawTabs(list){
    tabs.innerHTML='';list.forEach(function(a){var b=document.createElement('button');b.textContent=a==='ALL'?'ALL':'@'+a;
      if(a===filter)b.className='on';b.onclick=function(){filter=a;render()};tabs.appendChild(b)});
  }
  function renderApi(){
    drawTabs(['ALL'].concat(cfg.accounts));
    var posts=(cfg.posts||[]).filter(function(p){return filter==='ALL'||p.username.toLowerCase()===filter.toLowerCase()});
    var h=posts.map(function(p){return '<a class="xf-post" href="'+esc(p.url)+'" target="_blank" rel="noopener">'+
      '<div class="xf-who">'+(p.avatar?'<img src="'+esc(p.avatar)+'" alt="">':'')+'<b>'+esc(p.name)+'</b><span>@'+esc(p.username)+' · '+ago(p.created_at)+'</span></div>'+
      '<div class="xf-text">'+esc(p.text)+'</div><div class="xf-meta">♥ '+p.likes+'  ⟲ '+p.reposts+'</div></a>'}).join('');
    if(!h)h='<div class="xf-note">No posts yet.</div>';
    if(cfg.error)h='<div class="xf-note">X API error: '+esc(cfg.error)+'</div>'+h;
    body.innerHTML=h;
  }
  function renderEmbed(){
    if(filter==='ALL'||cfg.accounts.indexOf(filter)<0)filter=cfg.accounts[0];
    drawTabs(cfg.accounts);
    body.innerHTML='<a class="twitter-timeline" data-theme="dark" data-chrome="noheader nofooter transparent" data-height="2000" href="https://twitter.com/'+esc(filter)+'">Posts by @'+esc(filter)+'</a>'+
      '<div class="xf-note">If nothing appears, you may need to be signed in to X in this browser.</div>';
    if(window.twttr&&twttr.widgets){twttr.widgets.load(body)}
    else if(!widgets){widgets=true;var s=document.createElement('script');s.async=true;s.src='https://platform.twitter.com/widgets.js';document.body.appendChild(s)}
  }
  function render(){if(!cfg)return;cfg.mode==='api'?renderApi():renderEmbed()}
  function load(){fetch('/api/xfeed',{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){
      var modeChanged=!cfg||cfg.mode!==d.mode;cfg=d;if(d.mode==='api'||modeChanged)render()})
    .catch(function(){body.innerHTML='<div class="xf-note">Feed unavailable.</div>'})}
  function open(){panel.classList.add('open');try{localStorage.setItem('xfOpen','1')}catch(e){}
    if(!cfg)load();if(!timer)timer=setInterval(function(){if(cfg&&cfg.mode==='api')load()},60000)}
  function close(){panel.classList.remove('open');try{localStorage.removeItem('xfOpen')}catch(e){}}
  document.getElementById('xf-tab').onclick=function(){panel.classList.contains('open')?close():open()};
  document.getElementById('xf-close').onclick=close;
  try{if(localStorage.getItem('xfOpen'))open()}catch(e){}
})();
</script>
"""


def init_xfeed(app, auth=None):
    """Register the feed API and inject the panel into every HTML page."""
    if auth:
        xfeed_api_protected = auth(xfeed_api)
        app.add_url_rule("/api/xfeed", "xfeed_api", xfeed_api_protected)
    else:
        app.register_blueprint(xfeed_bp)

    @app.after_request
    def _inject_xfeed(resp):
        try:
            if (resp.status_code == 200 and resp.mimetype == "text/html"
                    and not resp.direct_passthrough and not resp.is_streamed):
                html = resp.get_data(as_text=True)
                if "</body>" in html and 'id="xf-panel"' not in html:
                    i = html.rfind("</body>")
                    resp.set_data(html[:i] + XFEED_WIDGET + html[i:])
        except Exception as e:
            print(f"xfeed inject skipped: {e}")
        return resp
