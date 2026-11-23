// Shared UI pieces (not charts).
import { html, useMemo, useState } from "./lib.js";

// columns: [{ key, label, get, render?, num?, text? }]; detail(row) renders an expanded row.
export function SortTable({ columns, rows, initialSort, rowKey, detail, limit = 200, empty = "Nothing right now." }) {
  const [sort, setSort] = useState(initialSort || { key: columns[0].key, dir: -1 });
  const [open, setOpen] = useState(() => new Set());
  const col = columns.find((c) => c.key === sort.key) || columns[0];
  const sorted = useMemo(() => {
    const out = [...rows];
    out.sort((a, b) => {
      const x = col.get(a);
      const y = col.get(b);
      if (typeof x === "string" || typeof y === "string") return String(x ?? "").localeCompare(String(y ?? "")) * sort.dir;
      return ((x ?? -Infinity) - (y ?? -Infinity)) * sort.dir;
    });
    return out;
  }, [rows, sort.key, sort.dir]);
  if (!rows.length) return html`<p class="empty">${empty}</p>`;

  const toggle = (k) => {
    const next = new Set(open);
    next.has(k) ? next.delete(k) : next.add(k);
    setOpen(next);
  };
  const header = (c) => {
    const active = c.key === sort.key;
    const onClick = () => setSort(active ? { key: c.key, dir: -sort.dir } : { key: c.key, dir: c.text ? 1 : -1 });
    return html`<th class=${c.num ? "num" : ""} aria-sort=${active ? (sort.dir < 0 ? "descending" : "ascending") : "none"}>
      <button class="th-btn" onClick=${onClick}>${c.label}${active ? (sort.dir < 0 ? " ↓" : " ↑") : ""}</button></th>`;
  };
  return html`<div class="table-wrap"><table class="data">
    <thead><tr>${columns.map(header)}</tr></thead>
    <tbody>${sorted.slice(0, limit).map((r) => {
      const k = rowKey(r);
      const isOpen = open.has(k);
      const cells = columns.map((c) => html`<td class=${c.num ? "num" : ""}>${c.render ? c.render(r) : c.get(r)}</td>`);
      if (!detail) return html`<tr>${cells}</tr>`;
      return html`
        <tr class=${`row ${isOpen ? "open" : ""}`} tabindex="0" aria-expanded=${isOpen}
          onClick=${(e) => !e.target.closest("a,button") && toggle(k)}
          onKeyDown=${(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), toggle(k))}>${cells}</tr>
        ${isOpen && html`<tr class="detail-row"><td colspan=${columns.length}>${detail(r)}</td></tr>`}`;
    })}</tbody>
  </table>
  ${sorted.length > limit && html`<p class="more">Showing ${limit} of ${sorted.length.toLocaleString()}.</p>`}
  </div>`;
}

const ICONS = { good: "✓", critical: "!", warning: "!", serious: "!", neutral: "○", muted: "–" };

// Status never relies on color alone: icon + label.
export function StatusChip({ status = "neutral", children }) {
  return html`<span class=${`chip ${status}`}><span class="chip-icon" aria-hidden="true">${ICONS[status] || "○"}</span>${children}</span>`;
}

export function Segmented({ options, value, onChange, label }) {
  return html`<div class="segmented" role="radiogroup" aria-label=${label}>
    ${options.map((o) => html`<button role="radio" aria-checked=${o.value === value}
      class=${o.value === value ? "on" : ""} onClick=${() => onChange(o.value)}>${o.label}</button>`)}
  </div>`;
}

export function Empty({ title, children }) {
  return html`<div class="card empty-card"><h3>${title}</h3><div class="sub">${children}</div></div>`;
}

export function ExtLink({ href, children }) {
  return html`<a href=${href} target="_blank" rel="noopener">${children}</a>`;
}

export function Notes({ notes }) {
  return notes && notes.length ? html`<ul class="notes">${notes.map((n) => html`<li>${n}</li>`)}</ul>` : null;
}
