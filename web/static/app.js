// sharpbet web chat. Every number shown comes from the server (the same tools as chat.py);
// this file only lays it out.
(() => {
  const $ = (s) => document.querySelector(s);
  const messages = $("#messages"), empty = $("#empty"), input = $("#input"), form = $("#composer");
  const send = $("#send"), costPill = $("#costPill");
  const LOGO = '<svg viewBox="0 0 32 32"><path d="M9 20l5-8 4 5 5-7"/></svg>';
  const COPY = '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
  const CHECK = '<svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></svg>';

  let sessionId = sessionStorage.getItem("sharpbet_sid") || newId();
  let busy = false;

  function newId() {
    const id = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now()).replace(/-/g, "");
    sessionStorage.setItem("sharpbet_sid", id);
    return id;
  }

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Minimal, safe markdown for the model's short lead: paragraphs, line breaks, **bold**, bullets.
  function md(text) {
    return esc(text).split(/\n{2,}/).map((block) => {
      const lines = block.split("\n");
      if (lines.every((l) => /^\s*[-•] /.test(l))) {
        return "<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*[-•] /, ""))}</li>`).join("") + "</ul>";
      }
      return `<p>${lines.map(inline).join("<br>")}</p>`;
    }).join("");
  }
  const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const money = (n) => "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function scrollDown() { window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }); }

  function addUser(text) {
    empty.hidden = true;
    const el = document.createElement("div");
    el.className = "msg user";
    el.innerHTML = `<div class="bubble">${esc(text)}</div>`;
    messages.appendChild(el);
    scrollDown();
  }

  function addBot() {
    const el = document.createElement("div");
    el.className = "msg bot";
    el.innerHTML = `<div class="avatar">${LOGO}</div><div class="body">
      <div class="typing">Working on it<span class="dots"><i></i><i></i><i></i></span></div></div>`;
    messages.appendChild(el);
    scrollDown();
    return el.querySelector(".body");
  }

  // ---------- slip rendering (from structured data) ----------
  function legsHtml(t) {
    const legs = t.legs.map((l) => `<div class="leg">
        <span class="leg-sel">${esc(l.selection)}${l.market.startsWith("Moneyline") ? " ML" : ""}</span>
        ${t.boost ? `<span class="odds-was">${esc(l.odds)}</span><span class="odds-arrow">→</span><span class="odds-boosted">${esc(t.boost.to)}</span>`
                  : `<span class="leg-odds">${esc(l.odds)}</span>`}
        <span class="leg-when">${esc(l.game)}${l.when ? " · " + esc(l.when) : ""}</span>
      </div>`).join("");
    const boost = t.boost ? `<div class="boost-line">Apply ${esc(t.boost.label ? "your " + t.boost.label : "the boost")}, then check the app shows <b>${esc(t.boost.to)}</b></div>` : "";
    return `<div class="legs">${legs}</div>${boost}`;
  }

  function ticketsTable(tickets) {
    const rows = tickets.map((t) => `<tr class="${t.boost ? "boosted" : ""}">
        <td class="num">${t.n}</td>
        <td data-label="Book"><span class="book">${esc(t.book)}</span></td>
        <td data-label="Funding"><span class="badge ${t.funding === "Cash" ? "cash" : "bonus"}">${esc(t.funding)}</span>
          ${t.boost ? `<span class="badge boost">${esc(t.boost.label || "BOOST")}</span>` : ""}</td>
        <td data-label="Bet">${legsHtml(t)}</td>
        <td data-label="Odds" class="odds${t.boost ? " is-boosted" : ""}">${esc(t.odds)}</td>
        <td data-label="Stake" class="money">${money(t.stake)}</td>
        <td data-label="Returns" class="money ret">${money(t.returns)}</td>
        <td class="copy-cell"><button class="copy" type="button" title="Copy this bet" data-copy="${esc(t.copy)}">${COPY}</button></td>
      </tr>`).join("");
    return `<table class="tickets"><thead><tr><th>#</th><th>Book</th><th>Funding</th><th>Bet</th>
      <th>Odds</th><th>Stake</th><th>Returns</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  // A bonus grid: every ticket has the same games in the same order, so show one column per game
  // (matchup and start time in the header) and one compact row per ticket, as in the spec.
  function isGrid(tickets) {
    if (tickets.length < 3) return false;
    const games = tickets[0].legs.map((l) => l.game).join("|");
    return tickets[0].legs.length > 1 && tickets.every((t) => t.legs.map((l) => l.game).join("|") === games);
  }

  function gridTable(tickets) {
    const heads = tickets[0].legs.map((l) => `<th class="game-col"><div class="gh-game">${esc(l.game)}</div>
        <div class="gh-when">${esc(l.when || "")}</div></th>`).join("");
    const rows = tickets.map((t) => `<tr>
        <td class="num">${t.n}</td>
        ${t.legs.map((l, i) => `<td class="pick" data-label="${esc(tickets[0].legs[i].game)}">
          <span class="leg-sel">${esc(l.selection)}${l.market.startsWith("Moneyline") ? " ML" : ""}</span>
          <span class="leg-odds">${esc(l.odds)}</span></td>`).join("")}
        <td data-label="Odds" class="odds">${esc(t.odds)}</td>
        <td data-label="Stake" class="money">${money(t.stake)}</td>
        <td data-label="Returns" class="money ret">${money(t.returns)}</td>
        <td class="copy-cell"><button class="copy" type="button" title="Copy this bet" data-copy="${esc(t.copy)}">${COPY}</button></td>
      </tr>`).join("");
    const book = tickets[0].book, funding = tickets[0].funding;
    return `<div class="grid-meta">All ${tickets.length} bets: <b>${esc(book)}</b> · ${esc(funding)} · ${tickets[0].legs.length}-leg parlays</div>
      <div class="table-scroll"><table class="tickets grid"><thead><tr><th>#</th>${heads}<th>Odds</th><th>Stake</th><th>Returns</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  const NOTICE = `<div class="slip-notice"><span>Place every bet in one sitting; odds move fast</span>
    <span>Locks only at these exact odds</span><span>Stop and re-run if any price differs</span></div>`;

  const CHEVRON = `<svg viewBox="0 0 24 24" class="chev"><path d="M6 9l6 6 6-6"/></svg>`;

  function compareHtml(c, s) {
    const sel = Math.max(0, c.rows.findIndex((r) => r.best));
    // replies saved before rows carried their own slips: show the winner only, rows not clickable
    const pickable = c.rows.every((r) => r.slip);
    if (!pickable) {
      const { comparison, ...winner } = s;
      c = { ...c, rows: c.rows.map((r, i) => ({ ...r, slip: i === sel && !winner.empty ? winner : { empty: true, notes: [] } })) };
    }
    const rows = c.rows.map((r, i) => `<tr class="${pickable ? "cmp-row" : "cmp-static"}${r.best ? " best" : ""}${i === sel ? " selected" : ""}" data-opt="${i}"
          tabindex="0" role="button" aria-pressed="${i === sel}" title="Show the bets for this option">
        <td><div class="cmp-name">${esc(r.name)}${r.best ? ` <span class="badge best">Best</span>` : ""}</div>
          <div class="cmp-promos">${esc(r.promos)}</div></td>
        <td data-label="Locks" class="money ret">${money(r.locked_profit)}</td>
        <td data-label="Cash" class="money">${money(r.cash_required)}</td>
        <td data-label="Return" class="money">${esc(r.return_on_cash)}</td>
        <td class="cmp-pick">${pickable ? CHEVRON : ""}</td></tr>`).join("");
    const slips = c.rows.map((r, i) => `<div class="cmp-slip" data-opt="${i}"${i === sel ? "" : " hidden"}>
        <div class="cmp-showing">Bets for <b>${esc(r.name)}</b>${r.best ? " · best option" : ""}</div>
        ${slipHtml(r.slip)}</div>`).join("");
    return `<div class="cmp-group"><div class="slip compare">
      <div class="slip-head"><div><div class="lock-label">Which offer is worth more${c.max_cash !== null && c.max_cash !== undefined ? ` · up to ${money(c.max_cash)} cash` : ""}</div>
        <div class="cmp-verdict">${esc(c.verdict)}</div>
        ${c.note ? `<div class="cmp-note">${esc(c.note)}</div>` : ""}</div></div>
      <div class="section"><div class="table-scroll"><table class="tickets cmp"><thead><tr><th>Option</th><th>Locks</th><th>Cash</th><th>Return on cash</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>
        <p class="instruction cmp-foot">Same games, prices, books and cash limit for every option. ${pickable ? "Tap an option to see its bets." : "Ask again to see the other option's bets."}</p></div></div>
      ${slips}</div>`;
  }

  function pickOption(row) {
    const group = row.closest(".cmp-group"), i = row.dataset.opt;
    group.querySelectorAll(".cmp-row").forEach((r) => {
      const on = r.dataset.opt === i;
      r.classList.toggle("selected", on); r.setAttribute("aria-pressed", on);
    });
    group.querySelectorAll(".cmp-slip").forEach((s) => { s.hidden = s.dataset.opt !== i; });
  }

  function slipHtml(s) {
    if (!s) return "";
    if (s.comparison) return compareHtml(s.comparison, s);
    if (s.kind === "resolve") {
      return `<div class="slip">${NOTICE}
        <div class="slip-head"><div><div class="lock-label">Re-solved grid</div>
          <div class="lock-value">${money(s.locked_profit)}</div>
          <div class="lock-sub">was ${money(s.was)} · keep tickets ${s.keep.join(", ") || "none"} as placed</div></div></div>
        <div class="section"><div class="section-title">Place these now</div>${ticketsTable(s.tickets)}</div></div>`;
    }
    if (s.empty) {
      return `<div class="slip"><div class="empty-slip">Nothing locks a profit right now.
        ${(s.notes || []).map((n) => `<div>${esc(n)}</div>`).join("")}</div></div>`;
    }
    const cash = s.cash_required === 0 ? "No cash needed"
      : `<b>Cash to put down</b>${money(s.cash_required)}${s.max_cash ? ` <span class="limit">· limit ${money(s.max_cash)}</span>` : ""}`;
    const games = s.games_from ? (s.games_from === s.games_to ? s.games_from : `${s.games_from} – ${s.games_to}`) : "";
    const sections = s.sections.map((sec) => `<div class="section">
        <div class="section-head">
          <div class="section-title">${esc(sec.title)}</div>
          ${sec.cash !== undefined ? `<div class="subtotals">
            <span class="sub lock">${money(sec.locked_profit)} locked</span>
            <span class="sub">${money(sec.cash)} cash</span>
            <span class="sub">${(sec.return_on_cash * 100).toFixed(1)}% return</span></div>` : ""}
        </div>
        <p class="instruction">${esc(sec.instruction)}</p>${isGrid(sec.tickets) ? gridTable(sec.tickets) : ticketsTable(sec.tickets)}</div>`).join("");
    const warns = s.warnings.length ? `<details class="warnings" open><summary>Before you place</summary><ul>
        ${s.warnings.map((w) => `<li><b>${esc(w.about)}:</b> ${esc(w.text)}</li>`).join("")}</ul></details>` : "";
    const alts = s.alternatives.length ? `<div class="alts"><b>Other options</b><ul>
        ${s.alternatives.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>` : "";
    const unused = (s.unused || []).length ? `<div class="alts"><b>Boosts not used</b><ul>
        ${s.unused.map((u) => `<li>${esc(u.boost)}: ${esc(u.why)}.</li>`).join("")}</ul></div>` : "";
    const notes = (s.notes || []).length && s.sections.length > 1 ? `<span class="chip"><b>Boosts</b>${esc(s.notes[0].replace(/^Uses /, "").replace(/ boosts\.$/, ""))}</span>` : "";
    return `<div class="slip">${NOTICE}
      <div class="slip-head">
        <div><div class="lock-label">Locked profit</div><div class="lock-value">${money(s.locked_profit)}</div>
          <div class="lock-sub">if every bet is placed at these odds</div></div>
        <div class="chips">
          <span class="chip">${cash}</span>
          <span class="chip"><b>Books</b>${esc(s.books.join(", "))}</span>
          ${games ? `<span class="chip"><b>Games</b>${esc(games)}</span>` : ""}
          <span class="chip"><b>Prices</b>${esc(s.quoted_at)}</span>
          ${notes}
        </div>
      </div>${sections}${unused}${warns}${alts}</div>`;
  }

  function renderDone(body, ev, animate) {
    let html = `<div class="text">${md(ev.text || "")}</div>${slipHtml(ev.slip)}`;
    if (ev.untraced && ev.untraced.length) {
      html += `<div class="flag">These numbers didn't come from a tool: ${esc(ev.untraced.join(", "))}. Don't rely on them; ask again.</div>`;
    }
    html += `<div class="meta">$${Number(ev.cost).toFixed(3)} this reply</div>`;
    body.insertAdjacentHTML("beforeend", html);
    const slip = body.querySelector(".slip");
    if (slip && animate) slip.classList.add("reveal");
  }

  // Redraw the conversation after a refresh (the server keeps it until it restarts or New chat).
  async function restore() {
    try {
      const res = await fetch(`/api/history?session_id=${encodeURIComponent(sessionId)}`);
      const data = await res.json();
      if (!data.messages || !data.messages.length) return;
      for (const m of data.messages) {
        if (m.role === "user") { addUser(m.text); continue; }
        const el = document.createElement("div");
        el.className = "msg bot" + (m.type === "stopped" ? " was-stopped" : "");
        el.innerHTML = `<div class="avatar">${LOGO}</div><div class="body"></div>`;
        const body = el.querySelector(".body");
        if (m.type === "done") renderDone(body, m, false);
        else if (m.type === "stopped") body.innerHTML = `${m.text ? `<div class="text">${md(m.text)}</div>` : ""}
          <div class="stopped">Stopped. Nothing from this reply was saved; your message was.</div>`;
        else body.innerHTML = `<div class="flag error">${esc(m.error || "Error")}</div>`;
        messages.appendChild(el);
      }
      // A refresh mid-reply stops it; if the page reloads before the server records that, the last
      // message has no reply yet. Say so instead of leaving it blank.
      if (data.messages[data.messages.length - 1].role === "user") {
        const el = document.createElement("div");
        el.className = "msg bot was-stopped";
        el.innerHTML = `<div class="avatar">${LOGO}</div><div class="body">
          <div class="stopped">Interrupted by the refresh. Your message was kept; say "go ahead" to try again.</div></div>`;
        messages.appendChild(el);
      }
      costPill.textContent = money(data.total_cost);
      window.scrollTo({ top: document.body.scrollHeight });
    } catch {}
  }

  // ---------- sending (streamed) ----------
  const SEND_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
  const STOP_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/></svg>';
  let controller = null;

  function setBusy(on) {
    busy = on;
    send.innerHTML = on ? STOP_ICON : SEND_ICON;
    send.classList.toggle("stop", on);
    send.setAttribute("aria-label", on ? "Stop" : "Send");
    send.type = on ? "button" : "submit";
    send.disabled = false;
  }

  // Follow new text only while the user is already near the bottom; never yank them.
  const nearBottom = () => window.innerHeight + window.scrollY >= document.body.scrollHeight - 140;
  function follow(stick) { if (stick) window.scrollTo({ top: document.body.scrollHeight }); }
  function showStart(el) {
    const top = el.getBoundingClientRect().top + window.scrollY - 80;
    if (top < window.scrollY || top > window.scrollY + window.innerHeight * 0.6) window.scrollTo({ top, behavior: "smooth" });
  }

  async function sendMessage(text) {
    if (busy || !text.trim()) return;
    setBusy(true);
    addUser(text);
    const body = addBot();
    const msgEl = body.closest(".msg");
    const status = body.querySelector(".typing");
    let textEl = null, said = "", finished = false;
    controller = new AbortController();

    const onEvent = (ev) => {
      const stick = nearBottom();
      if (ev.type === "status") {
        status.firstChild.textContent = ev.text + " ";
        status.hidden = false;
        if (said && !said.endsWith("\n\n")) said += "\n\n";   // text before a tool call gets its own paragraph
      } else if (ev.type === "text") {
        if (!textEl) { textEl = document.createElement("div"); textEl.className = "text streaming"; body.insertBefore(textEl, status); }
        said += ev.delta;
        textEl.innerHTML = md(said);
        status.hidden = true;
        follow(stick);
      } else if (ev.type === "done") {
        finished = true;
        status.remove();
        if (textEl) textEl.remove();
        renderDone(body, ev, true);
        costPill.textContent = money(ev.total_cost);
        showStart(msgEl);   // land on the start of the answer, not the bottom of the chart
      } else if (ev.type === "stopped") {
        finished = true;
      } else if (ev.type === "error") {
        finished = true;
        status.remove();
        body.insertAdjacentHTML("beforeend", `<div class="flag error">${esc(ev.error)}</div>`);
        if (ev.total_cost !== undefined) costPill.textContent = money(ev.total_cost);
      }
    };

    try {
      const res = await fetch("/api/chat/stream", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });
      if (!res.ok || !res.body) {
        const data = await res.json().catch(() => ({}));
        onEvent({ type: "error", error: data.error || data.detail || "Request failed." });
      } else {
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
            const line = chunk.split("\n").find((l) => l.startsWith("data: "));
            if (line) onEvent(JSON.parse(line.slice(6)));
          }
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") onEvent({ type: "error", error: "Couldn't reach the server. Is it running?" });
    }
    if (!finished) {   // stopped by the user
      status.remove();
      if (textEl) textEl.classList.remove("streaming");
      msgEl.classList.add("was-stopped");
      body.insertAdjacentHTML("beforeend", `<div class="stopped">Stopped. Nothing from this reply was saved; your message was.</div>`);
    }
    controller = null;
    setBusy(false);
    input.focus();
  }

  function stopReply() {
    if (!controller) return;
    fetch("/api/stop", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }) }).catch(() => {});
    controller.abort();
  }
  send.addEventListener("click", (e) => { if (busy) { e.preventDefault(); stopReply(); } });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (busy) return;
    const text = input.value;
    input.value = ""; autosize();
    sendMessage(text);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; }
  input.addEventListener("input", autosize);

  document.querySelectorAll(".suggestion").forEach((b) =>
    b.addEventListener("click", () => sendMessage(b.dataset.prompt)));

  messages.addEventListener("click", (e) => {
    const row = e.target.closest(".cmp-row");
    if (row) pickOption(row);
  });
  messages.addEventListener("keydown", (e) => {
    const row = e.target.closest(".cmp-row");
    if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); pickOption(row); }
  });

  messages.addEventListener("click", async (e) => {
    const btn = e.target.closest(".copy");
    if (!btn) return;
    try { await navigator.clipboard.writeText(btn.dataset.copy); } catch { return; }
    btn.innerHTML = CHECK; btn.classList.add("done");
    setTimeout(() => { btn.innerHTML = COPY; btn.classList.remove("done"); }, 1400);
  });

  $("#newChat").addEventListener("click", async () => {
    if (busy) stopReply();
    try { await fetch("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }) }); } catch {}
    sessionId = newId();
    messages.innerHTML = ""; empty.hidden = false; costPill.textContent = "$0.00"; input.focus();
  });

  restore();
  input.focus();
})();
