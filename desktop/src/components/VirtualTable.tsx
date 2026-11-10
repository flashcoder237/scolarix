import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

// Tableau à défilement virtuel : seules les lignes visibles (et une marge) sont dans le DOM, quel que soit le
// nombre de lignes (50 000 relevés, 60 000 lignes de journal). Vrai <table class="grid"> : en-tête collant,
// colonnes fixes, mêmes styles que les autres tableaux. Lignes de hauteur fixe (calcul direct, sans mesure).

export interface VCol<R> {
  key: string;
  label: string;
  width?: number | string;           // largeur fixe (px) ou relative ; sans largeur : partage le reste
  render: (row: R, index: number) => ReactNode;
  sortable?: boolean;
  align?: "right" | "center";
  title?: (row: R) => string | undefined;
  className?: string;
}

export function VirtualTable<R>({ count, getRow, cols, rowHeight = 34, selected, onSelect, onActivate, sort, onSort, onRange,
  rowKey, empty, testid, label }: {
  count: number;
  getRow: (index: number) => R | undefined;      // undefined : ligne pas encore chargée (journal par pages)
  cols: VCol<R>[];
  rowHeight?: number;
  selected?: number | null;
  onSelect?: (index: number) => void;
  onActivate?: (index: number) => void;           // double-clic ou Entrée
  sort?: { key: string; dir: 1 | -1 } | null;
  onSort?: (key: string) => void;
  onRange?: (first: number, last: number) => void;
  rowKey?: (row: R | undefined, index: number) => string | number;
  empty?: ReactNode;
  testid?: string;
  label?: string;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const [top, setTop] = useState(0);
  const [height, setHeight] = useState(600);
  const head = rowHeight + 2;                     // hauteur approximative de l'en-tête (défilement de la sélection)

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setHeight(el.clientHeight));
    ro.observe(el);
    setHeight(el.clientHeight);
    return () => ro.disconnect();
  }, []);

  const overscan = 8;
  const first = Math.max(0, Math.floor(top / rowHeight) - overscan);
  const last = Math.min(count - 1, Math.ceil((top + height) / rowHeight) + overscan);
  useEffect(() => { if (count) onRange?.(first, last); }, [first, last, count]); // eslint-disable-line react-hooks/exhaustive-deps

  // la ligne choisie reste visible (clavier, sélection venue d'ailleurs)
  useEffect(() => {
    const el = wrap.current;
    if (!el || selected == null || selected < 0) return;
    const y = selected * rowHeight;
    if (y < el.scrollTop) el.scrollTop = y;
    else if (y + rowHeight > el.scrollTop + el.clientHeight - head) el.scrollTop = y + rowHeight - el.clientHeight + head;
  }, [selected]); // eslint-disable-line react-hooks/exhaustive-deps

  // nouveau résultat plus court : on ne reste pas « après la fin »
  useEffect(() => { const el = wrap.current; if (el && el.scrollTop > count * rowHeight) el.scrollTop = 0; }, [count, rowHeight]);

  const onKey = (e: React.KeyboardEvent) => {
    if (!count || !onSelect) return;
    const cur = selected ?? -1;
    const page = Math.max(1, Math.floor(height / rowHeight) - 1);
    const go = (i: number) => { e.preventDefault(); onSelect(Math.max(0, Math.min(count - 1, i))); };
    if (e.key === "ArrowDown") go(cur + 1);
    else if (e.key === "ArrowUp") go(cur - 1);
    else if (e.key === "PageDown") go(cur + page);
    else if (e.key === "PageUp") go(cur - page);
    else if (e.key === "Home" && e.ctrlKey) go(0);
    else if (e.key === "End" && e.ctrlKey) go(count - 1);
    else if (e.key === "Enter" && cur >= 0 && onActivate) { e.preventDefault(); onActivate(cur); }
  };

  const rows: ReactNode[] = [];
  for (let i = first; i <= last; i++) {
    const r = getRow(i);
    rows.push(
      <tr key={rowKey ? rowKey(r, i) : i} className={selected === i ? "sel" : undefined} style={{ height: rowHeight }}
        onClick={() => onSelect?.(i)} onDoubleClick={() => onActivate?.(i)} aria-selected={selected === i}>
        {r === undefined
          ? <td colSpan={cols.length} className="muted small">…</td>
          : cols.map((c) => <td key={c.key} className={`${c.className || ""}${c.align ? ` al-${c.align}` : ""}`} title={c.title?.(r)}>{c.render(r, i)}</td>)}
      </tr>,
    );
  }
  return (
    <div ref={wrap} className="table-wrap vt-wrap" tabIndex={0} onKeyDown={onKey} onScroll={(e) => setTop((e.target as HTMLDivElement).scrollTop)}
      data-testid={testid} aria-label={label} aria-rowcount={count}>
      <table className="grid vt">
        <colgroup>{cols.map((c) => <col key={c.key} style={c.width != null ? { width: c.width } : undefined} />)}</colgroup>
        <thead><tr>{cols.map((c) => {
          const on = sort && sort.key === c.key;
          return (
            <th key={c.key} className={`${c.sortable ? "sortable" : ""}${on ? " sorted" : ""}${c.align ? ` al-${c.align}` : ""}`}
              onClick={c.sortable && onSort ? () => onSort(c.key) : undefined}
              aria-sort={on ? (sort!.dir === 1 ? "ascending" : "descending") : undefined} title={c.sortable ? "Trier (cliquer à nouveau : ordre inverse)" : undefined}>
              {c.label}{on && <span className="arr">{sort!.dir === 1 ? " ↑" : " ↓"}</span>}
            </th>
          );
        })}</tr></thead>
        <tbody>
          {first > 0 && <tr className="vt-pad" aria-hidden="true" style={{ height: first * rowHeight }}><td colSpan={cols.length} /></tr>}
          {rows}
          {last < count - 1 && <tr className="vt-pad" aria-hidden="true" style={{ height: (count - 1 - last) * rowHeight }}><td colSpan={cols.length} /></tr>}
        </tbody>
      </table>
      {!count && empty && <div className="vt-empty">{empty}</div>}
    </div>
  );
}
