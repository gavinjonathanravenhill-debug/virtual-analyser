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
.ts-th{cursor:pointer;user-select:none;white-space:nowrap}
.ts-th:hover{color:#7fff6e}
.ts-ar{display:inline-block;margin-left:4px;font-size:.85em;opacity:.45}
.ts-th.ts-on .ts-ar{opacity:1;color:#7fff6e}
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

  // ---------- Column sorting: click any table header to sort ▼/▲ ----------
  var SKEY = 'ts-sort:' + location.pathname;
  function sload(){ try { return JSON.parse(localStorage.getItem(SKEY) || '{}'); } catch(e){ return {}; } }
  function ssave(o){ try { localStorage.setItem(SKEY, JSON.stringify(o)); } catch(e){} }
  function cellText(c){ return c ? (c.innerText || c.textContent || '').replace(/\s+/g,' ').trim() : ''; }
  var UNIT = { k: 1e3, m: 1e6, b: 1e9, t: 1e12 };
  function num(t){
    if (!t || /^(–|-|—|n\/a|none|\?)$/i.test(t)) return null;
    var a = t.match(/^(\d+(?:\.\d+)?)\s*(s|m|min|h|d|w)\s+ago$/i);
    if (a) { var mul = {s:1/60, m:1, min:1, h:60, d:1440, w:10080}[a[2].toLowerCase()]; return -parseFloat(a[1]) * mul; }
    var m = t.replace(/,/g,'').match(/^([+\-−~]?)\s*\$?\s*(\d+(?:\.\d+)?)\s*([kmbt])?(?![a-z0-9])/i);
    if (!m) return null;
    var v = parseFloat(m[2]) * (m[3] ? UNIT[m[3].toLowerCase()] : 1);
    return (m[1] === '-' || m[1] === '−') ? -v : v;
  }
  function headerRow(tb){
    var th = tb.tHead && tb.tHead.rows.length ? tb.tHead.rows[tb.tHead.rows.length - 1] : null;
    if (th) return th;
    var r = tb.rows[0];
    if (r && r.cells.length > 1 && Array.prototype.every.call(r.cells, function(c){ return c.tagName === 'TH'; })) return r;
    return null;
  }
  function sig(hr){ return Array.prototype.map.call(hr.cells, function(c){ return cellText(c).replace(/[▲▼⇅]/g,'').trim(); }).join('|').slice(0,200); }
  function dataRows(tb, hr){
    var n = hr.cells.length;
    return Array.prototype.filter.call(tb.rows, function(r){
      return r !== hr && !(tb.tHead && r.parentNode === tb.tHead) && r.cells.length >= Math.min(2, n) && r.cells.length >= n - 1 && !r.querySelector('th');
    });
  }
  function colIndex(hr, th){ var i = 0; for (var k = 0; k < hr.cells.length; k++) { if (hr.cells[k] === th) return i; i += hr.cells[k].colSpan || 1; } return -1; }
  function cellAt(r, idx){ var i = 0; for (var k = 0; k < r.cells.length; k++) { if (i === idx) return r.cells[k]; i += r.cells[k].colSpan || 1; if (i > idx) return null; } return null; }
  function sortTable(tb, hr, idx, dir){
    var rows = dataRows(tb, hr); if (rows.length < 2) return;
    var vals = rows.map(function(r){ var t = cellText(cellAt(r, idx)); return { r: r, t: t, n: num(t) }; });
    var withText = vals.filter(function(v){ return v.t && !/^(–|-|—|n\/a)$/i.test(v.t); });
    var numeric = withText.length && withText.filter(function(v){ return v.n !== null; }).length >= withText.length * 0.6;
    var sorted = vals.map(function(v, i){ v.i = i; return v; }).sort(function(a, b){
      var av = numeric ? a.n : (a.t || null), bv = numeric ? b.n : (b.t || null);
      if (av === null && bv === null) return a.i - b.i;
      if (av === null) return 1; if (bv === null) return -1;
      var c = numeric ? av - bv : String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: 'base' });
      return (dir === 'asc' ? c : -c) || a.i - b.i;
    }).map(function(v){ return v.r; });
    for (var i = 0; i < rows.length; i++) if (rows[i] !== sorted[i]) { place(rows, sorted); return; }
  }
  function paint(hr, idx, dir){
    Array.prototype.forEach.call(hr.cells, function(c){
      var ar = c.querySelector('.ts-ar'); if (!ar) return;
      var on = colIndex(hr, c) === idx;
      c.classList.toggle('ts-on', on);
      ar.textContent = on ? (dir === 'asc' ? '▲' : '▼') : '⇅';
    });
  }
  function setupTable(tb){
    if (tb.closest('#xf-panel')) return;
    var hr = headerRow(tb); if (!hr) return;
    var key = sig(hr), st = sload()[key];
    Array.prototype.forEach.call(hr.cells, function(c){
      if (c.querySelector('.ts-ar') || c.hasAttribute('onclick') || !cellText(c)) return;
      c.classList.add('ts-th'); c.title = c.title || 'Click to sort';
      var ar = document.createElement('span'); ar.className = 'ts-ar'; ar.textContent = '⇅'; c.appendChild(ar);
      c.addEventListener('click', function(e){
        if (e.target.closest('a,button,input,select')) return;
        var idx = colIndex(hr, c), o = sload(), cur = o[key];
        var dir = cur && cur.idx === idx && cur.dir === 'desc' ? 'asc' : 'desc';
        o[key] = { idx: idx, dir: dir }; ssave(o);
        busy = true; try { sortTable(tb, hr, idx, dir); paint(hr, idx, dir); } finally { busy = false; }
      });
    });
    if (st) { sortTable(tb, hr, st.idx, st.dir); paint(hr, st.idx, st.dir); }
  }

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
      document.querySelectorAll('table').forEach(setupTable);
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
