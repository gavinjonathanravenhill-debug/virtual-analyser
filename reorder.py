"""Up/down arrows on every chart and table panel, injected into every HTML page.

Each panel (a .card, the price panel, table wraps, etc.) gets a small ▲▼ in its
top-right corner. Clicking swaps it with the neighbouring panel in the same
container. The order is remembered per page in the browser (localStorage).
"""

REORDER_WIDGET = r"""
<!-- Panel reorder arrows (injected by reorder.py) -->
<style>
.ro-ctl{position:absolute;top:6px;right:6px;z-index:20;display:flex;gap:4px}
.ro-ctl button{all:unset;box-sizing:border-box;cursor:pointer;width:26px;height:24px;line-height:22px;text-align:center;font-size:13px;font-weight:700;
  border-radius:5px;background:#1a2040;border:1px solid #7fff6e;color:#7fff6e;font-family:system-ui,sans-serif;box-shadow:0 1px 4px rgba(0,0,0,.5)}
.ro-ctl button:hover{background:#7fff6e;color:#060810}
.ro-ctl button[disabled]{opacity:.3;cursor:default;border-color:#5a6480;color:#5a6480;background:#0d1120}
.ro-flash{outline:1px solid rgba(127,255,110,.6);outline-offset:2px;transition:outline-color .6s}
</style>
<script id="ro-script">
(function(){
  var SEL = '.card,.price-panel,.risk-card,.table-wrap,.whale-section,.hero';
  var SKIP = '.modal,.box,#xf-panel,.tp';
  var KEY = 'ro-order:' + location.pathname;
  function load(){ try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch(e){ return {}; } }
  function save(o){ try { localStorage.setItem(KEY, JSON.stringify(o)); } catch(e){} }

  function isUnit(el){ return el.nodeType === 1 && el.matches(SEL) && !el.closest(SKIP); }
  function unitsOf(p){ return Array.prototype.filter.call(p.children, isUnit); }
  function label(el){
    if (el.id) return '#' + el.id;
    var h = el.querySelector('h1,h2,h3,h4,.ct,.corr-title,.plabel,th');
    var t = h ? h.textContent.replace(/\s+/g,' ').trim().slice(0,40) : '';
    return (el.className.split(' ')[0] || el.tagName) + ':' + t;
  }
  function keys(list){
    var seen = {};
    return list.map(function(el){ var k = label(el); seen[k] = (seen[k]||0) + 1; return seen[k] > 1 ? k + '~' + seen[k] : k; });
  }
  var pid = 0;
  function parentKey(p){
    if (p.id) return '#' + p.id;
    if (!p.dataset.roPid) {
      // stable-ish: path of tag/index from body
      var path = [], n = p;
      while (n && n !== document.body && n.parentElement) {
        path.unshift(n.tagName + Array.prototype.indexOf.call(n.parentElement.children, n));
        n = n.parentElement;
      }
      p.dataset.roPid = path.join('/') || ('p' + (pid++));
    }
    return p.dataset.roPid;
  }

  // Put `ordered` into the slots the units currently occupy (other elements stay put)
  function place(current, ordered){
    var marks = current.map(function(el){ var m = document.createComment('ro'); el.parentNode.insertBefore(m, el); return m; });
    ordered.forEach(function(el, i){ marks[i].parentNode.insertBefore(el, marks[i]); });
    marks.forEach(function(m){ m.remove(); });
  }

  function applySaved(p, list){
    var saved = load()[parentKey(p)];
    if (!saved || list.length < 2) return list;
    var ks = keys(list);
    var idx = list.map(function(el, i){ var s = saved.indexOf(ks[i]); return { el: el, r: s < 0 ? 1e6 + i : s }; });
    var ordered = idx.slice().sort(function(a,b){ return a.r - b.r; }).map(function(x){ return x.el; });
    for (var i = 0; i < list.length; i++) if (ordered[i] !== list[i]) { place(list, ordered); return ordered; }
    return list;
  }

  function persist(p){
    var o = load(); o[parentKey(p)] = keys(unitsOf(p)); save(o);
  }

  function move(el, dir){
    var p = el.parentElement, list = unitsOf(p), i = list.indexOf(el), j = i + dir;
    if (j < 0 || j >= list.length) return;
    var ordered = list.slice(); ordered[i] = list[j]; ordered[j] = el;
    var y = el.getBoundingClientRect().top;
    place(list, ordered);
    persist(p);
    // keep the moved panel under the cursor
    window.scrollBy(0, el.getBoundingClientRect().top - y);
    el.classList.add('ro-flash'); setTimeout(function(){ el.classList.remove('ro-flash'); }, 600);
    refreshButtons(p);
  }

  function refreshButtons(p){
    var list = unitsOf(p);
    list.forEach(function(el, i){
      var c = el.querySelector(':scope>.ro-ctl'); if (!c) return;
      c.children[0].disabled = i === 0;
      c.children[1].disabled = i === list.length - 1;
    });
  }

  function addCtl(el){
    if (el.querySelector(':scope>.ro-ctl')) return;
    if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
    el.classList.add('ro-unit');
    var c = document.createElement('div'); c.className = 'ro-ctl';
    c.innerHTML = '<button type="button" title="Move up">▲</button><button type="button" title="Move down">▼</button>';
    c.children[0].onclick = function(e){ e.stopPropagation(); move(el, -1); };
    c.children[1].onclick = function(e){ e.stopPropagation(); move(el, 1); };
    el.insertBefore(c, el.firstChild);
  }

  var busy = false;
  function scan(){
    if (busy) return; busy = true;
    try {
      var parents = new Set();
      document.querySelectorAll(SEL).forEach(function(el){ if (isUnit(el) && el.parentElement) parents.add(el.parentElement); });
      parents.forEach(function(p){
        var list = unitsOf(p);
        if (list.length < 2) return;
        list = applySaved(p, list);
        list.forEach(addCtl);
        refreshButtons(p);
      });
    } finally { busy = false; }
  }

  var t = null;
  function soon(){ clearTimeout(t); t = setTimeout(scan, 150); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scan); else scan();
  new MutationObserver(function(muts){
    if (busy) return;
    for (var i = 0; i < muts.length; i++) {
      var m = muts[i];
      if (m.target.closest && m.target.closest('.ro-ctl')) continue;
      soon(); return;
    }
  }).observe(document.body, { childList: true, subtree: true });

  // Reset link: double-click any arrow with Alt held clears this page's saved order
  document.addEventListener('dblclick', function(e){
    if (e.altKey && e.target.closest('.ro-ctl')) { try { localStorage.removeItem(KEY); } catch(_){} location.reload(); }
  });
})();
</script>
"""


def init_reorder(app):
    @app.after_request
    def _inject_reorder(resp):
        try:
            if (resp.status_code == 200 and resp.mimetype == "text/html"
                    and not resp.direct_passthrough and not resp.is_streamed):
                html = resp.get_data(as_text=True)
                if "</body>" in html and 'id="ro-script"' not in html:
                    i = html.rfind("</body>")
                    resp.set_data(html[:i] + REORDER_WIDGET + html[i:])
        except Exception as e:
            print(f"reorder inject skipped: {e}")
        return resp
