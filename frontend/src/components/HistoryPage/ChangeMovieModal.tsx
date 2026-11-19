import { useCallback, useEffect, useRef, useState } from "react";
import { SvPanel, SvLabel, sv } from "../../app/components/synapse";
import {
  changeMovieIdentity,
  errorDetail,
  previewMovieIdentity,
  type MovieIdentityPreview,
} from "../../api/client";

interface SearchHit {
  tmdb_id: number;
  name: string;
  type: "tv" | "movie";
  year: string;
}

export interface ChangeMovieModalProps {
  jobId: number;
  currentTitle: string | null;
  currentYear: number | null;
  currentSource: string;
  currentConfidence: number;
  currentPath: string | null;
  onApplied: () => void;
  onClose: () => void;
}

const buttonBase: React.CSSProperties = {
  padding: "10px 16px",
  fontFamily: sv.mono,
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.16em",
  textTransform: "uppercase",
};

/**
 * Correct a completed movie's TMDB identity: search or enter an id, review the
 * exact file moves (dry run, nothing written), then confirm. The apply button is
 * enabled only for a clean preview, and the server re-checks the preview token.
 */
export function ChangeMovieModal({
  jobId,
  currentTitle,
  currentYear,
  currentSource,
  currentConfidence,
  currentPath,
  onApplied,
  onClose,
}: ChangeMovieModalProps) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [idText, setIdText] = useState("");
  const [preview, setPreview] = useState<MovieIdentityPreview | null>(null);
  const [previewId, setPreviewId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const handleClose = useCallback(() => {
    if (!busy) onClose();
  }, [busy, onClose]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") handleClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [handleClose]);

  useEffect(() => () => clearTimeout(timer.current), []);

  function onQuery(q: string) {
    setQuery(q);
    clearTimeout(timer.current);
    if (!q.trim()) {
      setHits([]);
      return;
    }
    timer.current = setTimeout(async () => {
      try {
        const resp = await fetch(`/api/tmdb/search?query=${encodeURIComponent(q)}`);
        if (resp.ok) {
          const data = (await resp.json()) as { results?: SearchHit[] };
          setHits((data.results ?? []).filter((r) => r.type === "movie"));
        }
      } catch {
        // search is a convenience; an id can still be typed
      }
    }, 400);
  }

  async function runPreview(tmdbId: number) {
    setLoading(true);
    setError(null);
    setPreview(null);
    try {
      setPreview(await previewMovieIdentity(jobId, tmdbId));
      setPreviewId(tmdbId);
      setHits([]);
    } catch (e) {
      setError(errorDetail(e));
    } finally {
      setLoading(false);
    }
  }

  async function apply() {
    if (!preview || previewId == null) return;
    setBusy(true);
    setError(null);
    try {
      await changeMovieIdentity(jobId, previewId, preview.token);
      onApplied();
      onClose();
    } catch (e) {
      setError(errorDetail(e));
    } finally {
      setBusy(false);
    }
  }

  const idValue = Number(idText);
  const idValid = Number.isInteger(idValue) && idValue > 0;
  const canApply = !!preview && preview.ok && !busy && !loading;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Change movie"
    >
      <div
        className="absolute inset-0"
        style={{ background: `${sv.bg0}d9`, backdropFilter: "blur(4px)" }}
        onClick={handleClose}
      />
      <div className="relative w-full max-w-2xl" style={{ maxHeight: "90vh", overflowY: "auto" }}>
        <SvPanel glow pad={0} style={{ background: `linear-gradient(180deg, ${sv.bg2}, ${sv.bg1})` }}>
          <div style={{ padding: 24, display: "flex", flexDirection: "column", gap: 16 }}>
            <h3
              style={{
                fontFamily: sv.display,
                fontWeight: 700,
                fontSize: 16,
                letterSpacing: "0.18em",
                textTransform: "uppercase",
                color: sv.cyanHi,
                margin: 0,
              }}
            >
              Change movie
            </h3>

            <div style={{ fontFamily: sv.mono, fontSize: 11, color: sv.inkDim, lineHeight: 1.6 }}>
              <div>
                Currently:{" "}
                <span style={{ color: sv.cyan }}>
                  {currentTitle ?? "Unknown"}
                  {currentYear ? ` (${currentYear})` : ""}
                </span>{" "}
                · {currentSource} · {Math.round(currentConfidence * 100)}%
              </div>
              {currentPath && <div style={{ wordBreak: "break-all" }}>{currentPath}</div>}
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <SvLabel size={10}>Search TMDB</SvLabel>
              <input
                aria-label="Search movies"
                value={query}
                onChange={(e) => onQuery(e.target.value)}
                placeholder="Title"
                style={{
                  background: sv.bg1,
                  border: `1px solid ${sv.lineMid}`,
                  color: sv.ink,
                  padding: "8px 10px",
                  fontFamily: sv.mono,
                  fontSize: 12,
                }}
              />
              {hits.length > 0 && (
                <ul style={{ listStyle: "none", margin: 0, padding: 0 }} aria-label="Search results">
                  {hits.slice(0, 8).map((h) => (
                    <li key={h.tmdb_id}>
                      <button
                        type="button"
                        onClick={() => {
                          setIdText(String(h.tmdb_id));
                          void runPreview(h.tmdb_id);
                        }}
                        style={{
                          width: "100%",
                          textAlign: "left",
                          padding: "6px 8px",
                          fontFamily: sv.mono,
                          fontSize: 12,
                          color: sv.ink,
                          background: "transparent",
                          border: `1px solid ${sv.line}`,
                          cursor: "pointer",
                        }}
                      >
                        {h.name} {h.year ? `(${h.year})` : ""} · #{h.tmdb_id}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  aria-label="TMDB movie ID"
                  inputMode="numeric"
                  value={idText}
                  onChange={(e) => setIdText(e.target.value)}
                  placeholder="or enter a TMDB movie ID"
                  style={{
                    flex: 1,
                    background: sv.bg1,
                    border: `1px solid ${sv.lineMid}`,
                    color: sv.ink,
                    padding: "8px 10px",
                    fontFamily: sv.mono,
                    fontSize: 12,
                  }}
                />
                <button
                  type="button"
                  disabled={!idValid || loading}
                  onClick={() => void runPreview(idValue)}
                  style={{
                    ...buttonBase,
                    color: sv.cyan,
                    border: `1px solid ${sv.cyan}`,
                    background: "transparent",
                    opacity: !idValid || loading ? 0.5 : 1,
                  }}
                >
                  {loading ? "Checking…" : "Preview"}
                </button>
              </div>
            </div>

            {preview && (
              <div
                style={{ display: "flex", flexDirection: "column", gap: 8 }}
                aria-label="Preview"
              >
                <SvLabel size={10}>
                  Will become: {preview.proposed.title}
                  {preview.proposed.year ? ` (${preview.proposed.year})` : ""}
                </SvLabel>
                <table style={{ fontFamily: sv.mono, fontSize: 11, color: sv.ink, width: "100%" }}>
                  <tbody>
                    {preview.moves.map((m) => (
                      <tr key={m.title_id}>
                        <td style={{ color: sv.inkDim, paddingRight: 8, verticalAlign: "top" }}>
                          {m.role}
                        </td>
                        <td style={{ wordBreak: "break-all" }}>
                          {m.src}
                          <br />→ <span style={{ color: sv.cyan }}>{m.dst}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {preview.blockers.map((b) => (
                  <div key={b} role="alert" style={{ fontFamily: sv.mono, fontSize: 11, color: sv.red }}>
                    ✕ {b}
                  </div>
                ))}
                {preview.warnings.map((w) => (
                  <div key={w} style={{ fontFamily: sv.mono, fontSize: 11, color: sv.amber }}>
                    ! {w}
                  </div>
                ))}
              </div>
            )}

            {error && (
              <div role="alert" style={{ fontFamily: sv.mono, fontSize: 11, color: sv.red }}>
                {error}
              </div>
            )}

            <div style={{ display: "flex", gap: 8, borderTop: `1px solid ${sv.line}`, paddingTop: 16 }}>
              <button
                type="button"
                onClick={handleClose}
                disabled={busy}
                style={{
                  ...buttonBase,
                  flex: 1,
                  color: sv.inkDim,
                  border: `1px solid ${sv.lineMid}`,
                  background: "transparent",
                  opacity: busy ? 0.5 : 1,
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={apply}
                disabled={!canApply}
                style={{
                  ...buttonBase,
                  flex: 2,
                  color: sv.cyan,
                  border: `1px solid ${canApply ? sv.cyan : `${sv.cyan}33`}`,
                  background: canApply ? `${sv.cyan}1f` : "transparent",
                  cursor: canApply ? "pointer" : "not-allowed",
                  opacity: canApply ? 1 : 0.5,
                }}
              >
                {busy ? "Applying…" : "Apply movie correction"}
              </button>
            </div>
          </div>
        </SvPanel>
      </div>
    </div>
  );
}
