import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Search,
  X,
  Loader2,
  Download,
  Play,
  Copy,
  Check,
  ExternalLink,
  Users,
  Database,
  AlertCircle,
  SlidersHorizontal
} from 'lucide-react';
import { api, TorrentSearchResult } from '../api/client.ts';
import { formatBytes } from '../utils/formatters.ts';

type SeedrSearchFile = {
  id: string;
  streamId?: string;
  name: string;
  size: number;
  folderId: string;
  folderPath: string;
};

interface TorrentSearchPanelProps {
  onPrepare: (
    result: TorrentSearchResult,
    metadata?: {
      name: string;
      hash: string;
      files: { index: number; name: string; size: number; path: string; type: string; priority?: number }[];
      totalSize: number;
    }
  ) => Promise<{ files: SeedrSearchFile[]; deletedFolderIds?: string[] }>;
  onCancelPrepare?: () => void | Promise<void>;
  onOpenProgress?: () => void;
  seedrFiles?: SeedrSearchFile[];
  seedrDeletedFolderIds?: string[];
  onPlaySeedrFile?: (file: SeedrSearchFile) => void | Promise<void>;
}

function formatPublished(value?: string) {
  if (!value) return 'Unknown date';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Unknown date';
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export const TorrentSearchPanel: React.FC<TorrentSearchPanelProps> = ({ onPrepare, onCancelPrepare, onOpenProgress, seedrFiles = [], seedrDeletedFolderIds = [], onPlaySeedrFile }) => {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<TorrentSearchResult[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState('');
  // Seed count is the default ranking so the strongest swarms appear first.
  // 720p/1080p are mutually exclusive. Size and Time are independent sort toggles.
  const [resolutionFilter, setResolutionFilter] = useState<'720p' | '1080p' | null>(null);
  const [sizeSort, setSizeSort] = useState<'asc' | 'desc' | null>(null);
  const [timeSort, setTimeSort] = useState<'desc' | 'asc' | null>(null);
  const [showRecentSearches, setShowRecentSearches] = useState(false);
  const [preparingTorrentKey, setPreparingTorrentKey] = useState<string | null>(null);
  const [playingTorrentKey, setPlayingTorrentKey] = useState<string | null>(null);
  const [copiedTorrentKey, setCopiedTorrentKey] = useState<string | null>(null);
  const [prepareWaitTitle, setPrepareWaitTitle] = useState('');
  const [prepareWaitOpen, setPrepareWaitOpen] = useState(false);
  const [prepareError, setPrepareError] = useState('');

  // Metadata is prefetched in small batches so search remains fast while the
  // most likely results are already resolved when the user clicks Add.
  const metadataCacheRef = useRef(new Map<string, {
    name: string;
    hash: string;
    files: { index: number; name: string; size: number; path: string; type: string; priority?: number }[];
    totalSize: number;
  }>());
  const metadataInFlightRef = useRef(new Set<string>());
  const prefetchGenerationRef = useRef(0);
  const recentSearchRef = useRef<HTMLDivElement | null>(null);
  const searchRequestRef = useRef<AbortController | null>(null);
  const searchGenerationRef = useRef(0);

  const normalizeMatchText = (value: string) =>
    String(value || '')
      .toLowerCase()
      .replace(/\.[a-z0-9]{2,5}$/i, '')
      .replace(/[^a-z0-9]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();

  const findPreparedFiles = (result: TorrentSearchResult): SeedrSearchFile[] => {
    const title = normalizeMatchText(result.title);
    if (!title || title.length < 4) return [];

    const exact: SeedrSearchFile[] = [];
    const related: SeedrSearchFile[] = [];

    for (const file of seedrFiles) {
      const folderPath = String(file.folderPath || '');
      const folderName = folderPath.split('/').filter(Boolean).pop() || '';
      const fileName = normalizeMatchText(file.name);
      const folder = normalizeMatchText(folderName);

      if (folder === title || fileName === title) {
        exact.push(file);
        continue;
      }

      if (title.length >= 8 && (folder.includes(title) || title.includes(folder) || fileName.includes(title))) {
        related.push(file);
      }
    }

    return exact.length > 0 ? exact : related;
  };

  const preparedForResult = (result: TorrentSearchResult): SeedrSearchFile[] => {
    const key = result.infoHash || result.magnetUrl || result.downloadUrl || result.sourceUrl || result.title;
    const deletedIds = new Set(seedrDeletedFolderIds.map(id => String(id)));
    const local = preparedByKeyRef.current.get(key);
    const localFiles = local?.files?.filter(file => !deletedIds.has(String(file.folderId))) || [];
    return localFiles.length ? localFiles : findPreparedFiles(result);
  };

  const preparedByKeyRef = useRef(new Map<string, { files: SeedrSearchFile[] }>());
  const apiFetchRecent = (input: RequestInfo | URL, init?: RequestInit) => {
    const base = (String(import.meta.env.VITE_API_URL || '').trim() || 'https://torrent-studio-vercel-render-seedr-26fd.onrender.com').replace(/\/+$/, '');
    const value = String(input);
    return fetch(value.startsWith('/') ? base + value : value, init);
  };

  const [recentSearches, setRecentSearches] = useState<string[]>(() => {
    try {
      const saved = localStorage.getItem('seedflow_recent_searches');
      const parsed = saved ? JSON.parse(saved) : [];
      return Array.isArray(parsed)
        ? parsed.filter((value): value is string => typeof value === 'string').slice(0, 10)
        : [];
    } catch {
      return [];
    }
  });

  useEffect(() => {
    let cancelled = false;

    apiFetchRecent('/api/search/recent')
      .then(response => response.ok ? response.json() : null)
      .then(data => {
        if (cancelled) return;
        const serverRecents = Array.isArray(data?.searches)
          ? data.searches.filter((value: unknown): value is string => typeof value === 'string').slice(0, 7)
          : [];
        if (serverRecents.length > 0) {
          setRecentSearches(serverRecents);
          try {
            localStorage.setItem('seedflow_recent_searches', JSON.stringify(serverRecents));
          } catch {}
        }
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!showRecentSearches) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (target && !recentSearchRef.current?.contains(target)) {
        setShowRecentSearches(false);
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [showRecentSearches]);

  const saveRecentSearch = (value: string) => {
    const normalized = value.trim();
    if (!normalized) return;

    setRecentSearches(prev => {
      const next = [
        normalized,
        ...prev.filter(item => item.toLowerCase() !== normalized.toLowerCase())
      ].slice(0, 7);

      try {
        localStorage.setItem('seedflow_recent_searches', JSON.stringify(next));
      } catch {}

      void apiFetchRecent('/api/search/recent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ search: normalized })
      }).catch(() => {});

      return next;
    });
  };








  const runSearch = async (event?: React.FormEvent) => {
    event?.preventDefault();

    const trimmed = query.trim();
    if (trimmed.length < 2) {
      setError('Enter at least 2 characters to search.');
      setResults([]);
      setSearched(false);
      return;
    }

    const generation = ++searchGenerationRef.current;
    searchRequestRef.current?.abort();
    const controller = new AbortController();
    searchRequestRef.current = controller;

    try {
      setIsSearching(true);
      setShowRecentSearches(false);
      setError('');
      setResults([]);
      setSearched(false);
      saveRecentSearch(trimmed);

      // The backend owns low-result TV/season fallback. Keeping that logic
      // server-side avoids launching duplicate season searches from the browser.
      const data = await api.searchTorrents(trimmed, 50, controller.signal);

      // Never let an older request overwrite a newer search. This matters
      // when a slow 1337x fallback finishes after a later click.
      if (generation !== searchGenerationRef.current) return;

      setResults(data);
      setSearched(true);

      // Do not wait for metadata before displaying results. Start resolving
      // the first two results immediately, then the next two after that batch
      // finishes. The cache is used by Add when available.
      const prefetchGeneration = ++prefetchGenerationRef.current;
      void (async () => {
        const candidates = data.slice(0, 4);
        for (let start = 0; start < candidates.length; start += 2) {
          if (prefetchGenerationRef.current !== prefetchGeneration) return;

          const batch = candidates.slice(start, start + 2);
          await Promise.allSettled(batch.map(async (result) => {
            const source = result.magnetUrl || result.downloadUrl || result.sourceUrl;
            const key = result.infoHash || source || result.title;
            if (!source || !key || metadataCacheRef.current.has(key) || metadataInFlightRef.current.has(key)) {
              return;
            }

            metadataInFlightRef.current.add(key);
            try {
              const metadata = await api.inspectMagnet(
                source,
                'Downloads',
                result.infoUrl || result.sourceUrl || '',
                result.descriptorUrl || ''
              );

              if (
                metadata &&
                !metadata.pending &&
                Array.isArray(metadata.files) &&
                metadata.files.length > 0
              ) {
                metadataCacheRef.current.set(key, {
                  name: String(metadata.name || '').trim(),
                  hash: String(metadata.hash || result.infoHash || '').trim(),
                  files: metadata.files,
                  totalSize: Number(metadata.totalSize || 0)
                });
              }
            } catch {
              // Add will simply resolve this result on demand if prefetch fails.
            } finally {
              metadataInFlightRef.current.delete(key);
            }
          }));
        }
      })();

      if (data.length === 0) {
        setError('No matching torrent results were found.');
      }
    } catch (err: any) {
      if (generation !== searchGenerationRef.current) return;
      if (err?.name === 'AbortError') return;
      setResults([]);
      setSearched(true);
      setError(err?.message || 'Torrent search failed.');
    } finally {
      if (generation === searchGenerationRef.current) {
        setIsSearching(false);
        if (searchRequestRef.current === controller) {
          searchRequestRef.current = null;
        }
      }
    }
  };

  const sortedResults = useMemo(() => {
    const maxSeedrFriendlySize = 2 * 1024 * 1024 * 1024;
    const sorted = results.filter(result => {
      const size = Number(result.size) || 0;
      if (size > maxSeedrFriendlySize) return false;
      if (resolutionFilter) {
        const title = String(result.title || '');
        const pattern = resolutionFilter === '720p' ? /(?:^|[^0-9])720p(?:[^0-9]|$)/i : /(?:^|[^0-9])1080p(?:[^0-9]|$)/i;
        if (!pattern.test(title)) return false;
      }
      return true;
    });
    sorted.sort((a, b) => {
      const aSize = Number(a.size) || 0;
      const bSize = Number(b.size) || 0;
      const aTime = a.publishDate ? new Date(a.publishDate).getTime() : 0;
      const bTime = b.publishDate ? new Date(b.publishDate).getTime() : 0;
      if (sizeSort) {
        const comparison = sizeSort === 'asc' ? aSize - bSize : bSize - aSize;
        if (comparison !== 0) return comparison;
      }
      if (timeSort) {
        const comparison = timeSort === 'asc' ? aTime - bTime : bTime - aTime;
        if (comparison !== 0) return comparison;
      }
      return (Number(b.seeders) || 0) - (Number(a.seeders) || 0);
    });
    return sorted;
  }, [results, resolutionFilter, sizeSort, timeSort]);

  return (
    <div className="space-y-2.5 sm:space-y-4">
      <div className="p-2.5 sm:p-5 rounded-xl sm:rounded-2xl bg-slate-900 border border-slate-800">
        <div className="flex flex-col gap-1">
          <h2 className="text-base font-bold text-slate-100 flex items-center gap-2">
            <Search className="w-5 h-5 text-cyan-400" />
            Search Torrents
          </h2>
          <p className="hidden sm:block text-xs text-slate-400">
            Search cached torrent indexes for movies and TV. Search works independently of the Render backend.
          </p>
        </div>

        <form data-torrent-search="true" onSubmit={runSearch} className="mt-2.5 sm:mt-4 flex flex-row gap-1.5 sm:gap-2">
          <div ref={recentSearchRef} className="relative flex-1">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
            <input
              value={query}
              onFocus={() => {
                if (!searched && recentSearches.length > 0) setShowRecentSearches(true);
              }}
              onChange={(e) => {
                setQuery(e.target.value);
                if (error) setError('');
                if (recentSearches.length > 0) setShowRecentSearches(true);
              }}
              placeholder="Search movies, TV, music, software..."
              className="w-full pl-9 pr-10 py-2 sm:py-2.5 rounded-lg sm:rounded-xl bg-slate-950 border border-slate-800 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500/20"
            />
            {query && (
              <button
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  setQuery('');
                  setError('');
                  setSearched(false);
                  setResults([]);
                  setShowRecentSearches(recentSearches.length > 0);
                }}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded-lg text-slate-500 hover:text-slate-200 hover:bg-slate-800 transition"
                title="Clear search"
                aria-label="Clear search"
              >
                <X className="w-4 h-4" />
              </button>
            )}

            {showRecentSearches && recentSearches.length > 0 && (
              <div className="absolute left-0 right-0 top-full mt-2 z-30 rounded-xl border border-slate-700 bg-slate-900 shadow-2xl overflow-hidden">
                <div className="px-3 py-2 border-b border-slate-800">
                  <span className="text-[11px] font-semibold text-slate-400">Recent Searches</span>
                </div>
                <div className="max-h-72 overflow-y-auto">
                  {recentSearches.slice(0, 7).map((search, index) => (
                    <div
                      key={search}
                      className="flex items-center gap-1 border-b border-slate-800/70 last:border-b-0 hover:bg-slate-800 transition"
                    >
                      <button
                        type="button"
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => {
                          setQuery(search);
                          setError('');
                          setShowRecentSearches(false);
                          // A recent search is already a known-good query, so
                          // run it immediately instead of making the user press
                          // Search again.
                          window.setTimeout(() => {
                            const form = document.querySelector('form[data-torrent-search="true"]') as HTMLFormElement | null;
                            form?.requestSubmit();
                          }, 0);
                        }}
                        className="min-w-0 flex-1 px-3 py-2.5 text-left flex items-center gap-2.5 active:bg-slate-700 transition"
                      >
                        <span className="w-5 h-5 shrink-0 rounded-md bg-slate-800 text-slate-500 text-[10px] font-bold flex items-center justify-center">
                          {index + 1}
                        </span>
                        <span className="truncate text-xs text-slate-200">{search}</span>
                      </button>

                      <button
                        type="button"
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => {
                          setRecentSearches(prev => {
                            const next = prev.filter(item => item !== search);
                            try {
                              localStorage.setItem('seedflow_recent_searches', JSON.stringify(next));
                            } catch {}
                            return next;
                          });
                        }}
                        className="mr-2 p-1.5 rounded-lg text-slate-500 hover:text-slate-200 hover:bg-slate-700 transition shrink-0"
                        title="Delete recent search"
                        aria-label={`Delete recent search: ${search}`}
                      >
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          <button
            type="submit"
            disabled={isSearching}
            className="w-10 sm:w-auto px-2 sm:px-4 py-2 rounded-lg sm:rounded-xl bg-cyan-500 hover:bg-cyan-400 disabled:opacity-50 disabled:cursor-not-allowed text-slate-950 text-xs font-bold flex items-center justify-center gap-2 transition"
          >
            {isSearching ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                <span className="hidden sm:inline">Searching...</span>
              </>
            ) : (
              <>
                <Search className="w-4 h-4" />
                <span className="hidden sm:inline">Search</span>
              </>
            )}
          </button>
        </form>


      </div>

      {isSearching && (
        <div className="p-5 sm:p-7 rounded-xl sm:rounded-2xl bg-slate-900 border border-slate-800 flex flex-col items-center justify-center gap-3">
          <Loader2 className="w-8 h-8 text-cyan-400 animate-spin" />
          <div className="text-sm font-semibold text-slate-200">Searching torrents...</div>
          <div className="text-xs text-slate-500 text-center">Checking the fastest media sources and waiting for results.</div>
        </div>
      )}

      {prepareError && (
        <div className="p-2.5 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-200 text-xs flex items-center justify-between gap-2">
          <span>{prepareError}</span>
          <button type="button" onClick={() => setPrepareError('')} className="shrink-0 p-1 rounded text-slate-400 hover:text-slate-200" aria-label="Dismiss preparation error">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {prepareWaitOpen && (
        <div className="fixed inset-0 z-[200] flex items-center justify-center bg-slate-950/45 backdrop-blur-sm p-4">
          <div className="w-full max-w-sm rounded-2xl border border-cyan-400/20 bg-slate-900/95 shadow-2xl p-5">
            <div className="flex items-start gap-3">
              <div className="shrink-0 rounded-xl bg-cyan-500/10 border border-cyan-500/20 p-2">
                <Loader2 className="w-5 h-5 text-cyan-300 animate-spin" />
              </div>
              <div className="min-w-0">
                <div className="font-bold text-slate-100 text-sm">Loading is taking a little longer</div>
                <div className="mt-1 text-xs leading-5 text-slate-400">
                  <span className="text-slate-200">{prepareWaitTitle || 'This torrent'}</span> is still being prepared by Seedr. You can explore the app or wait for it to finish.
                </div>
              </div>
            </div>
            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={() => setPrepareWaitOpen(false)}
                className="flex-1 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 px-3 py-2 text-xs font-semibold transition"
              >
                Explore
              </button>
              <button
                type="button"
                onClick={() => {
                  setPrepareWaitOpen(false);
                  onOpenProgress?.();
                }}
                className="flex-1 rounded-xl bg-cyan-500 hover:bg-cyan-400 text-slate-950 px-3 py-2 text-xs font-bold transition"
              >
                View loading progress
              </button>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="p-3.5 rounded-2xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs flex items-start gap-2.5">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <div>
            <div className="font-semibold">{error}</div>
            {error.toLowerCase().includes('configured') ? (
              <div className="text-amber-400/80 mt-1">
                Try the full movie or series title, and include a year or season/episode when needed.
              </div>
            ) : null}
          </div>
        </div>
      )}

      {results.length > 0 && (
        <div className="space-y-2">
          <div className="rounded-xl border border-slate-800 bg-slate-900 px-2.5 py-2.5">
            <div className="flex items-center gap-2 overflow-x-auto">
              <span className="shrink-0 px-1 text-[10px] font-bold uppercase tracking-wider text-slate-500">Filters</span>
              <button type="button" onClick={() => setResolutionFilter(current => current === '720p' ? null : '720p')} className={resolutionFilter === '720p' ? 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950 shadow-[0_0_14px_rgba(34,211,238,0.45)]' : 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={resolutionFilter === '720p'}>720p</button>
              <button type="button" onClick={() => setResolutionFilter(current => current === '1080p' ? null : '1080p')} className={resolutionFilter === '1080p' ? 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950 shadow-[0_0_14px_rgba(34,211,238,0.45)]' : 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={resolutionFilter === '1080p'}>1080p</button>
              <button type="button" onClick={() => setSizeSort(current => current === null ? 'asc' : current === 'asc' ? 'desc' : null)} className={sizeSort ? 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950 shadow-[0_0_14px_rgba(34,211,238,0.45)]' : 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={Boolean(sizeSort)}>Size {sizeSort === 'asc' ? '↑' : sizeSort === 'desc' ? '↓' : ''}</button>
              <button type="button" onClick={() => setTimeSort(current => current === null ? 'desc' : current === 'desc' ? 'asc' : null)} className={timeSort ? 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950 shadow-[0_0_14px_rgba(34,211,238,0.45)]' : 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={Boolean(timeSort)}>Time {timeSort === 'desc' ? '↓' : timeSort === 'asc' ? '↑' : ''}</button>
            </div>
          </div>

          <div className="flex items-center justify-between gap-2 px-1">
            <div className="text-xs text-slate-400">
              {sortedResults.length} of {results.length} result{results.length === 1 ? '' : 's'}
            </div>
          </div>

          <div className="rounded-2xl border border-slate-800 overflow-hidden bg-slate-900 divide-y divide-slate-800/80">
            {sortedResults.map((result, index) => (
              <div
                key={result.guid || result.infoHash || (result.title + '-' + index)}
                className="p-2.5 sm:p-4 hover:bg-slate-900/80 transition"
              >
                <div className="flex flex-row items-center gap-2 sm:gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start gap-2">
                      <div className="p-1.5 sm:p-2 rounded-lg sm:rounded-xl bg-cyan-500/10 border border-cyan-500/20 shrink-0">
                        <Database className="w-3.5 h-3.5 sm:w-4 sm:h-4 text-cyan-400" />
                      </div>
                      <div className="min-w-0">
                        <h3 className="text-[13px] sm:text-sm font-semibold text-slate-100 line-clamp-2">
                          {result.title}
                        </h3>
                        <div className="flex flex-wrap items-center gap-x-2 sm:gap-x-3 gap-y-0.5 mt-1 text-[10px] sm:text-[11px] text-slate-500">
                          <span>{result.indexer || 'Unknown indexer'}</span>
                          <span>{formatPublished(result.publishDate)}</span>
                          {result.protocol && <span className="uppercase">{result.protocol}</span>}
                          {result.infoHash && (
                            <span className="font-mono truncate max-w-[220px]" title={result.infoHash}>
                              {result.infoHash}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center gap-2 sm:gap-3 mt-2 sm:mt-3 text-[11px] sm:text-xs text-slate-400">
                      <span className="font-mono">{formatBytes(result.size)}</span>
                      <span className="flex items-center gap-1 text-emerald-400">
                        <Users className="w-3.5 h-3.5" />
                        {result.seeders} seeders
                      </span>
                      <span className="text-slate-500">{result.leechers} leechers</span>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    {(() => {
                      const source = result.magnetUrl || result.downloadUrl || result.sourceUrl;
                      const torrentKey = result.infoHash || source || result.title;
                      const isPreparing = preparingTorrentKey === torrentKey;
                      const preparedFiles = preparedForResult(result);
                      const primaryFile = preparedFiles.find(file =>
                        /\.(mkv|mp4|m4v|webm|mov|avi|m3u8|ts|mp3|wav|flac|aac|ogg|m4a)$/i.test(file.name)
                      ) || preparedFiles[0];

                      if (preparedFiles.length > 0 && primaryFile) {
                        const isPlaying = playingTorrentKey === torrentKey;
                        return (
                          <div className="flex items-center gap-1.5 shrink-0">
                            <button
                              type="button"
                              disabled={isPlaying}
                              onClick={async () => {
                                if (isPlaying) return;
                                setPlayingTorrentKey(torrentKey);
                                try {
                                  await onPlaySeedrFile?.(primaryFile);
                                } finally {
                                  setPlayingTorrentKey(current => current === torrentKey ? null : current);
                                }
                              }}
                              className="px-2 py-1.5 rounded-lg bg-emerald-400 text-slate-950 font-bold text-xs hover:bg-emerald-300 transition flex items-center gap-1 disabled:opacity-70 disabled:cursor-wait"
                              title={isPlaying ? "Opening stream…" : "Play from Seedr"}
                            >
                              {isPlaying
                                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                : <Play className="w-3.5 h-3.5" />}
                              <span className="hidden sm:inline">{isPlaying ? 'Loading…' : 'Play'}</span>
                            </button>

                            <button
                              type="button"
                              onClick={() => api.openSeedrFileDownload(primaryFile.id, primaryFile.name)}
                              className="p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white transition"
                              title="Download file"
                              aria-label="Download file"
                            >
                              <Download className="w-3.5 h-3.5" />
                            </button>

                            <button
                              type="button"
                              disabled={copiedTorrentKey === torrentKey}
                              onClick={async () => {
                                try {
                                  const data = await api.getSeedrFileDownload(primaryFile.id);
                                  await navigator.clipboard.writeText(data.url);
                                  setCopiedTorrentKey(torrentKey);
                                  window.setTimeout(() => {
                                    setCopiedTorrentKey(current => current === torrentKey ? null : current);
                                  }, 2000);
                                } catch {
                                  setPrepareError('Could not copy the download link.');
                                }
                              }}
                              className="p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-cyan-400 transition disabled:opacity-60"
                              title="Copy download link"
                              aria-label="Copy download link"
                            >
                              {copiedTorrentKey === torrentKey
                                ? <Check className="w-3.5 h-3.5 text-emerald-400" />
                                : <Copy className="w-3.5 h-3.5" />}
                            </button>
                          </div>
                        );
                      }

                      return (
                        <div className="flex items-center gap-1.5 shrink-0">
                          <button
                            type="button"
                            disabled={!source || isPreparing}
                            onClick={async () => {
                              if (!source || isPreparing) return;

                              setPrepareError('');
                              setPreparingTorrentKey(torrentKey);
                              setPrepareWaitTitle(result.title);
                              setPrepareWaitOpen(false);
                              const longWaitTimer = window.setTimeout(() => setPrepareWaitOpen(true), 30000);

                              try {
                                const metadata = metadataCacheRef.current.get(torrentKey);
                                const prepared = await onPrepare(result, metadata);
                                const deletedFolderIds = new Set(
                                  (prepared?.deletedFolderIds || []).map(id => String(id).trim()).filter(Boolean)
                                );

                                // A new Prepare may have triggered automatic Seedr cleanup.
                                // Remove the deleted folders from the local "prepared" map first,
                                // otherwise stale Play/Download/Copy buttons would remain visible
                                // even after the Seedr files themselves disappear.
                                if (deletedFolderIds.size > 0) {
                                  for (const [key, entry] of preparedByKeyRef.current.entries()) {
                                    if (entry.files.some(file => deletedFolderIds.has(String(file.folderId)))) {
                                      preparedByKeyRef.current.delete(key);
                                    }
                                  }
                                }

                                if (prepared?.files?.length) {
                                  preparedByKeyRef.current.set(torrentKey, { files: prepared.files });
                                }
                                setPrepareWaitOpen(false);
                              } catch (error: any) {
                                setPrepareError(String(error?.message || 'Could not prepare this torrent.'));
                              } finally {
                                window.clearTimeout(longWaitTimer);
                                setPreparingTorrentKey(current => current === torrentKey ? null : current);
                              }
                            }}
                            className="px-2.5 sm:px-3.5 py-1.5 sm:py-2 rounded-lg sm:rounded-xl bg-cyan-500 hover:bg-cyan-400 disabled:opacity-60 disabled:cursor-not-allowed text-slate-950 text-xs font-bold flex items-center gap-1.5 transition"
                          >
                            {isPreparing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                            <span>{isPreparing ? 'Preparing…' : 'Prepare'}</span>
                          </button>
                          {isPreparing && onCancelPrepare && (
                            <button
                              type="button"
                              onClick={() => void onCancelPrepare()}
                              className="px-2 py-1.5 rounded-lg bg-rose-500/10 border border-rose-500/25 text-rose-300 hover:bg-rose-500/20 text-[10px] font-bold transition"
                              title="Cancel preparation"
                            >
                              Cancel
                            </button>
                          )}
                        </div>
                      );
                    })()}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {!isSearching && searched && sortedResults.length === 0 && !error && (
        <div className="py-14 text-center rounded-2xl bg-slate-900 border border-slate-800">
          <Search className="w-10 h-10 text-slate-700 mx-auto mb-3" />
          <h3 className="text-sm font-bold text-slate-300">
            {results.length > 0 ? 'No results match your filters' : 'No results'}
          </h3>
          <p className="text-xs text-slate-500 mt-1">
            {results.length > 0
              ? 'Try a different resolution or sorting filter.'
              : 'Try a broader search term or change your filters.'}
          </p>
        </div>
      )}
    </div>
  );
};
