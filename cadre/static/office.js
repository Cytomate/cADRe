// cADRe office: every agent is a robot in one shared office. Built from DOM elements (not a
// canvas) so names and actions sit in their own tags and never overlap the scene, and the
// light/dark colours come from the CSS variables in index.html.
//   working   -> at its project's desk, screen scrolling, bobbing as it types
//   waiting   -> at its desk with a hand up and a "!" (needs you)
//   idle      -> leaves its desk and roams the free floor (corridors, empty spots, open floor)
//   suspended -> frozen where it stands, greyed, pause mark
//   sleeping  -> on a lounge couch (recent sessions; a click copies the resume command)
//   installed -> on a charging pad in the dock
//   cADRe     -> the police droid: patrols past every desk and stops next to any flagged agent
(function () {
  const C = window.cadre;
  const floor = document.getElementById("floor");
  if (!C || !floor) return;
  const desksEl = document.getElementById("desks"), loungeEl = document.getElementById("lounge");
  const dockEl = document.getElementById("dock"), openEl = document.getElementById("openfloor");
  const botsEl = document.getElementById("bots"), tip = document.getElementById("tip"), doorEl = document.getElementById("door");
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const {esc, short, agoShort, dur, nameOf, statusLabel, botSVG, copSVG, agentCard, WARN} = C;
  const rand = (a, b) => a + Math.random() * (b - a);

  let snap = null, byId = new Map(), geo = null, lastDesks = "", lastLounge = "", lastDock = "";
  const bots = new Map();          // id -> moving robot (live agents + the cop), kept across snapshots
  let hoverId = null, pinnedId = null;

  // ------------------------------------------------------------------ desks
  // One desk per project folder used in the last DESK_DAYS days, plus every folder a live agent is
  // in now (older projects stay in the Projects table). Working CLI agents without a folder share
  // a "Lobby" desk. GUI apps and model runtimes get no desk at all: their working dir means
  // nothing (/, C:\Windows\System32) and their "working" is only CPU, which comes and goes, so
  // they roam instead (a desk for them would appear and vanish with their CPU).
  const DESK_DAYS = 7;
  const NO_DESK_KINDS = new Set(["desktop", "runtime"]);
  const LIVE = new Set(["working", "waiting", "idle", "suspended"]);
  const pathKey = p => /^[A-Za-z]:|\\/.test(p) ? p.replace(/[\\/]+$/, "").toLowerCase() : (p.replace(/\/+$/, "") || "/");
  const deskKey = a => NO_DESK_KINDS.has(a.kind) ? null : (a.cwd && a.project) ? pathKey(a.cwd) : "__lobby";
  const isLive = a => a.pid != null && LIVE.has(a.status);
  function desksFor(s) {
    const map = new Map(), now = s.generated_at || Date.now() / 1000;
    for (const p of s.projects || []) {
      if (p.last_activity && now - p.last_activity <= DESK_DAYS * 86400) map.set(pathKey(p.path), {key: pathKey(p.path), name: p.name, project: p});
    }
    for (const a of s.agents || []) {
      const k = deskKey(a);
      if (k && k !== "__lobby" && isLive(a) && !map.has(k)) map.set(k, {key: k, name: a.project});
    }
    if ((s.agents || []).some(a => isLive(a) && (a.status === "working" || a.status === "waiting") && deskKey(a) === "__lobby")) {
      map.set("__lobby", {key: "__lobby", name: "Lobby"});
    }
    return [...map.values()];
  }
  const seatedHere = a => a.status === "working" || a.status === "waiting" || a.status === "suspended";

  function nowText(a) {
    if (a.status === "waiting") return a.waiting_for || "needs your input";
    if (a.status === "suspended") return "suspended (stopped with Ctrl+Z or a debugger)";
    if (a.status === "idle") return "away from desk" + (a.last_prompt ? ` · last: “${short(a.last_prompt, 60)}”` : "");
    return a.last_tool || a.last_prompt || "working";
  }
  function tagHTML(a) {
    const meta = [a.model_label || a.model, a.cpu != null ? a.cpu + "% CPU" : "", a.rss_mb != null ? Math.round(a.rss_mb) + " MB" : "",
                  a.uptime_s != null ? "up " + dur(a.uptime_s) : ""].filter(Boolean);
    return `<button type="button" class="tag st-${esc(a.status)}${(a.flags || []).length ? " flagged" : ""}" data-id="${esc(a.id)}">
      <span class="tag-head"><span class="pill st-${esc(a.status)}">${esc(statusLabel(a.status))}</span><b class="ell">${esc(a.display_name || a.agent)}</b>${a.git_branch ? `<span class="branch ell">${esc(a.git_branch)}</span>` : ""}</span>
      <span class="tag-now mono ell"><i>›</i>${esc(nowText(a))}</span>
      ${(a.flags || []).slice(0, 2).map(f => `<span class="tag-flag">${WARN}<span class="ell">${esc(f)}</span></span>`).join("")}
      ${meta.length ? `<span class="tag-meta ell">${meta.map(esc).join(" · ")}</span>` : ""}</button>`;
  }
  function renderDesks(s, desks, assigned) {
    const html = desks.map(d => {
      const ags = assigned.get(d.key) || [];
      const scr = ags.some(a => a.status === "working") ? "work" : ags.some(a => a.status === "waiting") ? "wait" : "off";
      const p = d.project;
      const tags = ags.length ? ags.map(tagHTML).join("")
        : `<div class="tag empty-desk">No agent here${p && p.last_activity ? " · last active " + esc(agoShort(p.last_activity)) + " ago" : ""}${p && p.git_branch ? " · " + esc(p.git_branch) : ""}</div>`;
      return `<div class="desk" data-key="${esc(d.key)}"><div class="desk-scene">
          <span class="cubicle"></span><span class="monitor"><span class="screen ${scr}"><i></i><i></i><i></i></span></span>
          ${ags.some(seatedHere) ? "" : '<span class="chair"></span>'}
          <span class="desk-top"></span><span class="desk-front"><span>${esc(d.name)}</span></span>
          <span class="desk-leg l"></span><span class="desk-leg r"></span></div>
        <div class="desk-tags">${tags}</div></div>`;
    }).join("") || `<p class="empty">No projects with agent activity in the last ${DESK_DAYS} days.</p>`;
    if (html !== lastDesks) { desksEl.innerHTML = html; lastDesks = html; }
  }

  function renderLounge(s) {
    const sleepers = (s.agents || []).filter(a => a.status === "sleeping");
    const shown = sleepers.slice(0, 9), couches = [];
    for (let i = 0; i < Math.max(1, shown.length); i += 3) couches.push(shown.slice(i, i + 3));
    const seat = a => `<button type="button" class="seat" data-id="${esc(a.id)}" aria-label="${esc(nameOf(a) + " · " + (a.project || "") + (a.resume_cmd ? " · copy resume command" : ""))}">
        <span class="sleeper">${botSVG(a, "sleep", 44, 51)}<span class="zz">z</span></span>
        <span class="seat-name"><b>${esc(short(nameOf(a), 14))}</b><br><span>${esc(short(a.project || "?", 12))} · ${esc(agoShort(a.last_activity))}</span></span></button>`;
    const html = `<div class="zone-head"><b>Lounge</b><span>${sleepers.length ? `${sleepers.length} asleep · click one to copy its resume command` : "nobody napping"}</span></div>` +
      couches.map(c => `<div class="couch"><div class="seats">${c.map(seat).join("")}</div></div>`).join("") +
      (sleepers.length > shown.length ? `<span class="more-n">+${sleepers.length - shown.length} more</span>` : "");
    if (html !== lastLounge) { loungeEl.innerHTML = html; lastLounge = html; }
  }

  function renderDock(s) {
    const parked = (s.installed || []).filter(i => !i.active);
    const html = `<div class="zone-head"><b>Charging dock · installed</b><span>${parked.length ? parked.length + " not running" : "everything installed is in use"}</span></div>
      <div class="pads">${parked.map(i => `<button type="button" class="pad" data-id="installed:${esc(i.agent_key)}">
        <span class="spot">${botSVG({family: i.family, status: "installed"}, "dock", 44, 51)}</span>
        <b>${esc(i.agent)}</b><span>${esc(i.kind)}</span></button>`).join("")}</div>`;
    if (html !== lastDock) { dockEl.innerHTML = html; lastDock = html; }
  }

  // ------------------------------------------------------------------ geometry (from the laid-out DOM)
  function measure() {
    const fr = floor.getBoundingClientRect();
    const rel = el => { const r = el.getBoundingClientRect(); return {l: r.left - fr.left, t: r.top - fr.top, r: r.right - fr.left, b: r.bottom - fr.top}; };
    const desks = new Map(), boxes = [];
    for (const el of desksEl.querySelectorAll(".desk")) {
      const sc = rel(el.querySelector(".desk-scene")), box = rel(el);
      desks.set(el.dataset.key, {cx: (sc.l + sc.r) / 2, bottom: sc.b, top: sc.t});
      boxes.push(box);
    }
    const area = rel(desksEl), open = rel(openEl), door = rel(doorEl), lounge = rel(loungeEl);
    const zones = [{x0: open.l + 80, x1: open.r - 50, y0: open.t + 96, y1: open.b - 8}];   // the open floor
    const rows = [];
    for (const b of boxes) {
      const row = rows.find(r => Math.abs(r.t - b.t) < 6);
      row ? (row.b = Math.max(row.b, b.b), row.r = Math.max(row.r, b.r)) : rows.push({t: b.t, b: b.b, r: b.r});
    }
    rows.sort((p, q) => p.t - q.t);
    for (let i = 0; i + 1 < rows.length; i++) {          // corridors between desk rows
      const gap = rows[i + 1].t - rows[i].b;
      if (gap > 40) zones.push({x0: area.l + 40, x1: area.r - 40, y0: rows[i].b + Math.min(96, gap - 4), y1: rows[i + 1].t + 6});
    }
    // (no roaming inside a desk row: getting there would mean walking through that row's tags)
    if (!boxes.length) zones.push({x0: area.l + 40, x1: area.r - 40, y0: area.t + 100, y1: area.b - 10});
    const route = [];
    for (const d of desks.values()) route.push({x: Math.min(d.cx + 128, fr.width - 64), y: d.bottom - 2});
    route.push({x: open.r - 60, y: open.b - 10}, {x: open.l + 70, y: open.b - 10}, {x: lounge.r + 4, y: (lounge.t + lounge.b) / 2 + 40});
    // the aisle: the gap between the lounge and the desks (or the left edge when stacked), where
    // robots change rows so they never walk across the agent tags under the desks
    geo = {desks, zones, route, door: {x: (door.l + door.r) / 2, y: 46}, w: fr.width,
           aisle: Math.max(32, (boxes.length ? Math.min(...boxes.map(b => b.l)) : area.l + 70) - 35)};
  }
  function wanderPoint() {
    const zs = geo.zones.filter(z => z.x1 > z.x0 && z.y1 >= z.y0);
    if (!zs.length) return {x: geo.door.x, y: geo.door.y + 60};
    const total = zs.reduce((n, z) => n + (z.x1 - z.x0) * Math.max(8, z.y1 - z.y0), 0);
    let pick = Math.random() * total;
    for (const z of zs) {
      pick -= (z.x1 - z.x0) * Math.max(8, z.y1 - z.y0);
      if (pick <= 0) return {x: rand(z.x0, z.x1), y: rand(z.y0, z.y1)};
    }
    const z = zs[0]; return {x: rand(z.x0, z.x1), y: rand(z.y0, z.y1)};
  }

  // ------------------------------------------------------------------ robots
  function planPath(from, to) {
    if (Math.abs(from.y - to.y) < 12) return [to];
    return [{x: geo.aisle, y: from.y}, {x: geo.aisle, y: to.y}, to];
  }
  function setTarget(b, p) {         // re-plan only when the destination really moved
    if (b.path && Math.hypot(p.x - b.tx, p.y - b.ty) < 4) return;
    b.tx = p.x; b.ty = p.y;
    b.path = reduceMotion ? [] : planPath({x: b.x, y: b.y}, p);
  }
  function makeBot(id, cop) {
    const el = document.createElement("div");
    el.className = "bot" + (cop ? " cop" : "");
    el.dataset.id = id;
    botsEl.appendChild(el);
    const b = {id, el, cop, x: geo.door.x, y: geo.door.y, tx: geo.door.x, ty: geo.door.y, pause: 0, wp: 0, sig: "",
               pose: "walk", tagText: "cADRe · on patrol", w: cop ? 56 : 60, h: cop ? 74 : 70};
    bots.set(id, b);
    return b;
  }
  function paint(b) {               // re-render the robot only when what it shows changes
    const a = b.data;
    const tagText = b.cop ? b.tagText : `<b>${esc(nameOf(a))}</b><span> · ${esc(short(a.project || statusLabel(a.status), 16))}${a.status === "idle" ? " · idle " + esc(agoShort(a.last_activity)) : ""}</span>`;
    const sig = [b.cop ? "cop" : a.status, a.family, b.pose, tagText].join("|");
    if (sig === b.sig) return;
    b.sig = sig;
    b.el.innerHTML = `<span class="bot-tag">${tagText}</span>` + (b.cop ? copSVG()
      : botSVG(a, b.pose) + '<span class="alert">!</span><span class="pause">❚❚</span>');
    b.el.setAttribute("aria-label", b.cop ? "cADRe" : `${a.display_name || a.agent} · ${statusLabel(a.status)}`);
  }
  function place(b) {
    b.el.style.transform = `translate(${Math.round(b.x - b.w / 2)}px, ${Math.round(b.y - b.h)}px)`;
    b.el.style.zIndex = Math.round(b.y);
  }

  function sync(s) {
    snap = s;
    byId = new Map((s.agents || []).map(a => [a.id, a]));
    for (const i of s.installed || []) byId.set("installed:" + i.agent_key, {...i, id: "installed:" + i.agent_key, status: "installed"});
    const desks = desksFor(s), keys = new Set(desks.map(d => d.key));
    const live = (s.agents || []).filter(isLive);
    const assigned = new Map();
    for (const a of live) {
      const k = deskKey(a);
      if (k && keys.has(k)) (assigned.get(k) || assigned.set(k, []).get(k)).push(a);
    }
    renderDesks(s, desks, assigned); renderLounge(s); renderDock(s);
    measure();

    const seen = new Set(), seatIx = new Map();
    for (const a of live) {
      seen.add(a.id);
      const b = bots.get(a.id) || makeBot(a.id, false);
      b.data = a;
      const k = deskKey(a), desk = k && geo.desks.get(k);
      if (desk && seatedHere(a)) {
        const n = seatIx.get(k) || 0; seatIx.set(k, n + 1);
        const seat = {x: desk.cx + 12 + [0, 52, -46, 96][n % 4], y: desk.bottom - 24};
        if (a.status === "suspended") {             // frozen in its chair
          b.x = b.tx = seat.x; b.y = b.ty = seat.y; b.path = []; b.mode = "frozen"; b.pose = "desk"; b.el.classList.add("seated");
        } else { b.mode = "seat"; setTarget(b, seat); }
      } else if (a.status === "suspended") {
        b.mode = "frozen"; b.tx = b.x; b.ty = b.y; b.path = [];
      } else {
        if (b.mode !== "wander") { b.path = null; setTarget(b, wanderPoint()); b.pause = 0; }
        b.mode = "wander";
      }
      if (reduceMotion) {
        b.x = b.tx; b.y = b.ty;
        if (b.mode === "seat") { b.pose = "desk"; b.el.classList.add("seated"); } else b.el.classList.remove("seated");
      }
      b.el.classList.toggle("waiting", a.status === "waiting");
      b.el.classList.toggle("suspended", a.status === "suspended");
      b.el.classList.toggle("flagged", (a.flags || []).length > 0);
      paint(b); place(b);
    }
    // the tracker itself: a police droid on patrol
    seen.add("tracker");
    const cop = bots.get("tracker") || makeBot("tracker", true);
    if (!cop.placed) { cop.x = cop.tx = (geo.route[0] || geo.door).x; cop.y = cop.ty = (geo.route[0] || geo.door).y; cop.path = []; cop.placed = true; }
    cop.data = {id: "tracker", ...(s.tracker || {})};
    cop.mode = "patrol";
    for (const [id, b] of bots) if (!seen.has(id)) { b.el.remove(); bots.delete(id); }
    if (pinnedId && !findTarget(pinnedId)) unpin();
    if (pinnedId) showTip(pinnedId);
  }

  function suspectSpot(f) {
    const b = bots.get(f.id);
    if (b) {
      const k = deskKey(b.data), desk = k && geo.desks.get(k);
      if (b.mode === "seat" && desk) return {x: Math.min(desk.cx + 128, geo.w - 64), y: desk.bottom - 2};
      return {x: b.x + 52 > geo.w - 30 ? b.x - 52 : b.x + 52, y: b.y};
    }
    const el = floor.querySelector(`.seat[data-id="${CSS.escape(f.id)}"], .tag[data-id="${CSS.escape(f.id)}"]`);
    if (!el) return null;
    const fr = floor.getBoundingClientRect(), r = el.getBoundingClientRect();
    return {x: Math.min(geo.w - 30, r.right - fr.left + 26), y: r.bottom - fr.top};
  }
  function copStep(b) {
    const f = ((b.data.flagged) || []).map(f => ({f, spot: suspectSpot(f)})).find(x => x.spot);
    const alert = !!f;
    b.el.classList.toggle("alert", alert);
    b.tagText = alert ? `investigating ${esc(short(f.f.name, 18))}` : "cADRe · on patrol";
    paint(b);
    if (alert) { setTarget(b, f.spot); b.suspect = true; return; }
    b.suspect = false;
    const p = geo.route[b.wp % Math.max(1, geo.route.length)];
    if (p) setTarget(b, p);
  }

  function step(dt) {
    for (const b of bots.values()) {
      if (b.mode === "frozen") { b.el.classList.remove("moving"); continue; }
      if (b.cop) copStep(b);
      const goal = b.path && b.path.length ? b.path[0] : {x: b.tx, y: b.ty};
      b.el.classList.toggle("aisle", !!(b.path && b.path.length > 1));   // changing rows: tag hidden
      const dx = goal.x - b.x, dy = goal.y - b.y, d = Math.hypot(dx, dy);
      const speed = b.mode === "wander" ? 42 : b.cop ? 70 : 120;
      if (d > 1.5) {
        const v = Math.min(d, speed * dt);
        b.x += dx / d * v; b.y += dy / d * v;
        b.el.classList.add("moving");
        b.el.classList.remove("seated", "typing");
        if (Math.abs(dx) > 1) b.el.classList.toggle("face-left", dx < 0);
        if (!b.cop && b.pose !== "walk") { b.pose = "walk"; paint(b); }
      } else if (b.path && b.path.length) {
        b.path.shift();                            // next leg of the route
      } else {
        b.el.classList.remove("moving");
        if (b.mode === "seat") {
          if (b.pose !== "desk") { b.pose = "desk"; paint(b); }
          b.el.classList.add("seated"); b.el.classList.remove("face-left");
          b.el.classList.toggle("typing", b.data.status === "working");
        } else if (b.cop && !b.suspect) {
          b.pause -= dt;
          if (b.pause <= 0) { b.wp++; b.pause = rand(1.2, 2.6); }
        } else if (b.mode === "wander") {
          b.pause -= dt;
          if (b.pause <= 0) { setTarget(b, wanderPoint()); b.pause = rand(2, 6); }
        }
      }
      place(b);
    }
  }

  // ------------------------------------------------------------------ details on hover / click
  function findTarget(id) {
    return bots.get(id) ? bots.get(id).el : floor.querySelector(`[data-id="${CSS.escape(id)}"]`);
  }
  function cardFor(id) {
    if (id === "tracker") {
      const t = (snap && snap.tracker) || {};
      const fl = (t.flagged || []).map(f => `<span class="flag">${WARN}${esc(f.name)}: ${esc(f.flags.join(", "))}</span>`).join("") || '<p class="mute" style="margin:8px 0 0">No flagged agents right now.</p>';
      return `<div class="card-head"><span class="fam" style="background:#3B5BDB"></span><b>cADRe</b>${(t.flagged || []).length ? '<span class="pill" style="color:var(--flag-fg);background:var(--flag-bg)">investigating</span>' : '<span class="pill st-working">on patrol</span>'}</div>
        <div class="card-sub">The tracker itself · pid ${esc(t.pid)}</div>
        <dl class="kv"><dt>Watching</dt><dd>${esc(t.watching)} agents</dd><dt>Events</dt><dd>${esc(t.events_received)} from ${esc(t.hooked_sessions)} hooked session(s)</dd>
        <dt>Process</dt><dd>${esc(t.cpu)}% CPU · ${esc(t.rss_mb)} MB · up ${dur(t.uptime_s)}</dd>${t.paused ? "<dt>Recording</dt><dd>paused</dd>" : ""}</dl>${fl}`;
    }
    const a = byId.get(id);
    if (!a) return "";
    if (a.status === "installed") {
      return `<div class="card-head"><span class="fam" style="background:${C.famColor(a.family)}"></span><b>${esc(a.agent)}</b><span class="pill st-installed">installed</span></div>
        <div class="card-sub">${esc(a.kind)} · not running</div><dl class="kv"><dt>Found</dt><dd>${a.evidence.map(e => `<code>${esc(e)}</code>`).join("<br>")}</dd></dl>`;
    }
    return agentCard(a);
  }
  function showTip(id) {
    const el = findTarget(id), html = cardFor(id);
    if (!el || !html) { tip.hidden = true; return; }
    tip.innerHTML = html; tip.hidden = false;
    const fr = floor.getBoundingClientRect(), r = el.getBoundingClientRect(), tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = r.right - fr.left + 12;
    if (left + tw > fr.width - 8) left = Math.max(8, r.left - fr.left - tw - 12);
    const top = Math.max(8, Math.min(r.top - fr.top - 10, fr.height - th - 8));
    tip.style.left = left + "px"; tip.style.top = top + "px";
  }
  function mark() {
    for (const b of bots.values()) { b.el.classList.toggle("hover", b.id === hoverId); b.el.classList.toggle("pinned", b.id === pinnedId); }
  }
  function unpin() { pinnedId = null; tip.hidden = true; mark(); }

  floor.addEventListener("mouseover", e => {
    const t = e.target.closest("[data-id]");
    if (!t || !floor.contains(t)) return;
    hoverId = t.dataset.id; mark();
    if (!pinnedId) showTip(hoverId);
  });
  floor.addEventListener("mouseout", e => {
    const t = e.target.closest("[data-id]");
    if (t && !t.contains(e.relatedTarget)) { hoverId = null; mark(); if (!pinnedId) tip.hidden = true; }
  });
  floor.addEventListener("click", e => {
    if (tip.contains(e.target)) return;
    const t = e.target.closest("[data-id]");
    if (t && t.classList.contains("seat")) {            // sleeping session: copy how to resume it
      const a = byId.get(t.dataset.id);
      if (a && a.resume_cmd) C.copyText(a.resume_cmd, "resume command");
    }
    if (!t) return unpin();
    pinnedId = pinnedId === t.dataset.id ? null : t.dataset.id;
    pinnedId ? showTip(pinnedId) : (tip.hidden = true);
    mark();
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && pinnedId) unpin(); });

  window.officeFocus = id => {
    floor.scrollIntoView({behavior: reduceMotion ? "auto" : "smooth", block: "center"});
    pinnedId = id; mark();
    setTimeout(() => showTip(id), reduceMotion ? 0 : 450);
  };

  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => snap && sync(snap), 150); });

  let last = performance.now();
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    if (geo && !document.hidden && !reduceMotion) step(dt);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  window.officeUpdate = sync;
})();
