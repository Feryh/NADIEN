import { useEffect, useState } from 'react'

// Rows per page for every data table: 10, 30, 50, 100 or all (0). One choice for the whole app, remembered per
// browser, so a table opened later shows the same amount.

export const PAGE_SIZES = [10, 30, 50, 100, 0]
const PAGE_SIZE_KEY = 'nadien.pageSize'
const DEFAULT_PAGE_SIZE = 30
const CHANGE_EVENT = 'nadien:page-size'

function storedPageSize(): number {
  try {
    const raw = localStorage.getItem(PAGE_SIZE_KEY)
    const value = raw === null ? NaN : Number(raw)  // Number(null) is 0, which means "all"
    return PAGE_SIZES.includes(value) ? value : DEFAULT_PAGE_SIZE
  } catch {
    return DEFAULT_PAGE_SIZE
  }
}

/** The app-wide page size; changing it updates every table on screen. */
export function usePageSize(): [number, (size: number) => void] {
  const [pageSize, setState] = useState(storedPageSize)
  useEffect(() => {
    const sync = () => setState(storedPageSize())
    window.addEventListener(CHANGE_EVENT, sync)
    return () => window.removeEventListener(CHANGE_EVENT, sync)
  }, [])
  function setPageSize(size: number) {
    try {
      localStorage.setItem(PAGE_SIZE_KEY, String(size))
    } catch {
      /* private window / storage blocked: the choice just isn't remembered */
    }
    setState(size)
    window.dispatchEvent(new Event(CHANGE_EVENT))
  }
  return [pageSize, setPageSize]
}

export interface Paged<T> {
  pageRows: T[]
  page: number
  pages: number
  total: number
  pageSize: number
  setPage: (page: number) => void
  setPageSize: (size: number) => void
}

/** Client-side paging of rows already loaded; back to page 1 whenever `resetKey` (filters) changes. */
export function usePaged<T>(rows: T[], resetKey?: unknown): Paged<T> {
  const [pageSize, setPageSize] = usePageSize()
  const [page, setPage] = useState(0)
  useEffect(() => setPage(0), [resetKey, pageSize])
  const pages = pageSize ? Math.max(1, Math.ceil(rows.length / pageSize)) : 1
  const current = Math.min(page, pages - 1)
  return {
    pageRows: pageSize ? rows.slice(current * pageSize, (current + 1) * pageSize) : rows,
    page: current,
    pages,
    total: rows.length,
    pageSize,
    setPage,
    setPageSize,
  }
}

/** Count, rows-per-page choice and page buttons, under a table. */
export function Pager({ paged, unit = 'baris' }: { paged: Paged<unknown>; unit?: string }) {
  return (
    <PagerControls total={paged.total} page={paged.page} pages={paged.pages} pageSize={paged.pageSize}
      onPage={paged.setPage} onPageSize={paged.setPageSize} unit={unit} />
  )
}

/** Same controls for tables paged by the server (`offset`/`limit`). */
export function PagerControls({ total, page, pages, pageSize, onPage, onPageSize, unit = 'baris' }: {
  total: number
  page: number
  pages: number
  pageSize: number
  onPage: (page: number) => void
  onPageSize: (size: number) => void
  unit?: string
}) {
  return (
    <div className="pager">
      <span className="muted">{total.toLocaleString('id-ID')} {unit}</span>
      <label className="check">
        Tampilkan
        <select value={pageSize} onChange={(e) => onPageSize(Number(e.target.value))}>
          {PAGE_SIZES.map((size) => <option key={size} value={size}>{size || 'Semua'}</option>)}
        </select>
      </label>
      {pages > 1 && (
        <>
          <button type="button" disabled={page === 0} onClick={() => onPage(0)} title="Halaman pertama">«</button>
          <button type="button" disabled={page === 0} onClick={() => onPage(page - 1)} title="Sebelumnya">‹</button>
          <span>{page + 1} / {pages}</span>
          <button type="button" disabled={page >= pages - 1} onClick={() => onPage(page + 1)} title="Berikutnya">›</button>
          <button type="button" disabled={page >= pages - 1} onClick={() => onPage(pages - 1)} title="Halaman terakhir">»</button>
        </>
      )}
    </div>
  )
}
