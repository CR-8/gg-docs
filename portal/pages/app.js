// Shared helpers for the portal pages.
const NAV = [["/", "Overview"], ["/activity", "Activity"], ["/people", "People"], ["/reviews", "Reviews"],
             ["/chat", "Ask"], ["/settings", "Settings"]];

export const h = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
export const short = sha => (sha || "").slice(0, 8);
export const when = iso => iso ? new Date(iso).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"}) : "";
export const repoName = repo => (repo || "").split("/")[1] || repo || "";
export const commitLink = (repo, sha) => `<a href="https://github.com/${h(repo)}/commit/${h(sha)}" target="_blank" rel="noopener"><code>${short(sha)}</code></a>`;
export const prLink = (repo, n) => `<a href="https://github.com/${h(repo)}/pull/${n}" target="_blank" rel="noopener">#${n}</a>`;

export async function api(path, opts = {}) {
  const r = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  if (r.status === 401) { location.href = "/auth/login?next=" + encodeURIComponent(location.pathname); return new Promise(() => {}); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || r.statusText);
  return data;
}

export async function nav(active) {
  const me = await api("/api/me").catch(() => null);
  document.querySelector("nav").innerHTML =
    NAV.map(([href, name]) => `<a href="${href}"${href === active ? ' class="on"' : ""}>${name}</a>`).join("") +
    (me ? `<span class="me">${h(me.login)}${me.admin ? " (admin)" : ""} &middot; <a href="/auth/logout">sign out</a></span>` : "");
  return me;
}

export function checkChips(checks) {
  if (!checks || !checks.length) return '<span class="muted">no CI yet</span>';
  const failed = checks.filter(c => ["failure", "timed_out"].includes(c.conclusion));
  const passed = checks.filter(c => c.conclusion === "success").length;
  return `<span class="chip ${failed.length ? "bad" : "ok"}">${passed}/${checks.length} passed</span>` +
    failed.map(c => `<a class="chip bad" href="${h(c.url)}" target="_blank" rel="noopener">${h(c.name)}</a>`).join("");
}

// Sortable table: columns = [{k, label, num, fmt(row)}]
export function table(el, rows, columns, sortKey, desc = true) {
  let key = sortKey, down = desc;
  const draw = () => {
    const sorted = [...rows].sort((a, b) => {
      const x = a[key] ?? (down ? -Infinity : Infinity), y = b[key] ?? (down ? -Infinity : Infinity);
      return (x > y ? 1 : x < y ? -1 : 0) * (down ? -1 : 1);
    });
    el.innerHTML = `<div class="scroll"><table><thead><tr>${columns.map(c =>
      `<th data-k="${c.k}" class="${c.num ? "n" : ""} ${c.cls || ""}" title="${h(c.title || "")}">${h(c.label)}${c.k === key ? (down ? " ↓" : " ↑") : ""}</th>`).join("")}</tr></thead><tbody>${
      sorted.map(r => `<tr>${columns.map(c => `<td class="${c.num ? "n" : ""} ${c.cls || ""}">${c.fmt ? c.fmt(r) : h(r[c.k] ?? "")}</td>`).join("")}</tr>`).join("")
    }</tbody></table></div>`;
    el.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => {
      down = th.dataset.k === key ? !down : true; key = th.dataset.k; draw();
    });
  };
  draw();
}
