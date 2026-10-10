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
  SlidersHorizontal,
  Film
} from 'lucide-react';
import { api, API_BASE, MovieCatalogueKey, MovieCataloguePage, TorrentSearchResult } from '../api/client.ts';
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

interface TorrentQualityGroup {
  key: string;
  title: string;
  year: string;
  variants: TorrentSearchResult[];
  posterResult: TorrentSearchResult;
}

function torrentResultKey(result: TorrentSearchResult): string {
  return String(result.guid || result.infoHash || result.magnetUrl || result.downloadUrl || result.title);
}

function normalizeGroupTitle(value: string): string {
  return String(value || '')
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/[’']/g, '')
    .replace(/[._]+/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^(?:the|a|an)\s+/, '');
}

function torrentMediaIdentity(result: TorrentSearchResult): { title: string; normalized: string; year: string } {
  const source = String(result.mediaTitle || result.title || '').trim();
  const embeddedYear = source.match(/\b((?:19|20)\d{2})\b/);
  let year = String(result.year || embeddedYear?.[1] || '').trim();
  let title = source.replace(/[._]+/g, ' ');

  if (embeddedYear) {
    title = title.slice(0, embeddedYear.index);
  } else {
    // Release tags in brackets are often incomplete on public indexers.
    title = title
      .replace(/\[[^\]]*(?:\]|$)/g, ' ')
      .replace(/\([^)]*(?:\)|$)/g, ' ');
    title = title.split(
      /\b(?:2160p|1440p|1080p|720p|576p|480p|4k|8k|web[- ]?dl|web[- ]?rip|webrip|bluray|blu[- ]?ray|brrip|hdrip|dvdrip|dvdscr|telesync|telecine|r5|r6|cam|hdcam|x264|x265|h264|h265|hevc|xvid|divx|dts|aac|ac3|ddp|eng|english|nlsub|dual[ .-]?audio|hindi|tamil|telugu|malayalam|kannada|proper|repack|remux|hdr|bluelady|jaybob|haggis|voltage|dtrg|document|vision|sonido|saimorny|ltt)\b/i
    )[0];
  }

  title = title.replace(/\s+/g, ' ').trim().replace(/[\s._:[\](){}-]+$/g, '').trim();
  let normalized = normalizeGroupTitle(title);

  // Common LimeTorrents release shorthand: keep the sequel distinct from
  // The Avengers (2012), while presenting it with its catalogue title.
  if ((normalized === 'avengers 2' || normalized === 'the avengers 2') && (!year || year === '2015')) {
    title = 'Avengers: Age of Ultron';
    normalized = normalizeGroupTitle(title);
    year = year || '2015';
  } else if (
    (normalized === 'marvel s the avengers' || normalized === 'marvels the avengers') &&
    (!year || year === '2012')
  ) {
    title = 'The Avengers';
    normalized = normalizeGroupTitle(title);
    year = year || '2012';
  } else if (normalized === 'avengers age of ultron' && (!year || year === '2015')) {
    title = 'Avengers: Age of Ultron';
    year = year || '2015';
  } else if (normalized === 'the avengers' && year === '2012') {
    title = 'The Avengers';
  }

  if (!title) title = String(result.title || result.mediaTitle || 'Unknown title').trim();
  return { title, normalized: normalizeGroupTitle(title), year };
}

function torrentQualityDetails(result: TorrentSearchResult): { key: string; label: string; rank: number } {
  const raw = `${String(result.quality || '')} ${String(result.title || '')}`;
  const resolution = raw.match(/\b(2160p|1440p|1080p|720p|576p|480p|4k|8k)\b/i)?.[1]?.toLowerCase() || '';
  const resolutionLabel = resolution === '4k' ? '2160p (4K)' : resolution.toUpperCase();
  const formats: Array<[RegExp, string, number]> = [
    [/\b3d\b.*\b(?:bluray|blu[ .-]?ray)\b|\b(?:bluray|blu[ .-]?ray)\b.*\b3d\b/i, '3D BluRay', 900],
    [/\b(?:web[- .]?dl)\b/i, 'WEB-DL', 800],
    [/\b(?:web[- .]?rip|webrip)\b/i, 'WEBRip', 750],
    [/\b(?:blu[ .-]?ray|bluray)\b/i, 'BluRay', 700],
    [/\b(?:brrip)\b/i, 'BRRip', 650],
    [/\b(?:hdtv)\b/i, 'HDTV', 600],
    [/\b(?:hdrip)\b/i, 'HDRip', 550],
    [/\b(?:dvdscr|dvd[ .-]?scr)\b/i, 'DVDScr', 500],
    [/\b(?:dvdrip|dvd)\b/i, 'DVDRip', 450],
    [/\b(?:telesync|ts)\b/i, 'Telesync', 400],
    [/\b(?:telecine|tc)\b/i, 'Telecine', 350],
    [/\b(?:r5|r6)\b/i, 'R5/R6', 300],
    [/\b(?:hdcam|cam)\b/i, 'CAM', 200],
  ];
  const format = formats.find(([pattern]) => pattern.test(raw));
  const label = [resolutionLabel, format?.[1]].filter(Boolean).join(' ') || (resolutionLabel || 'Other release');
  const rank = (resolution === '2160p' || resolution === '4k' ? 2160
    : resolution === '1440p' ? 1440
    : resolution === '1080p' ? 1080
    : resolution === '720p' ? 720
    : resolution === '576p' ? 576
    : resolution === '480p' ? 480
    : resolution === '8k' ? 4320
    : 0) + (format?.[2] || 0);
  return { key: label.toLowerCase().replace(/[^a-z0-9]+/g, '-'), label, rank };
}

function groupTorrentResults(results: TorrentSearchResult[]): TorrentQualityGroup[] {
  const titleBuckets = new Map<string, Array<{ result: TorrentSearchResult; identity: ReturnType<typeof torrentMediaIdentity> }>>();
  for (const result of results) {
    const identity = torrentMediaIdentity(result);
    if (!identity.normalized) continue;
    const bucket = titleBuckets.get(identity.normalized) || [];
    bucket.push({ result, identity });
    titleBuckets.set(identity.normalized, bucket);
  }

  const groups: TorrentQualityGroup[] = [];
  for (const [normalized, entries] of titleBuckets) {
    const knownYears = Array.from(new Set(entries.map(entry => entry.identity.year).filter(Boolean)));
    const yearBuckets = new Map<string, typeof entries>();
    for (const entry of entries) {
      // Unknown-year releases can join a title only if this result set has one
      // unambiguous year for that title; don't merge remakes with one another.
      const year = entry.identity.year || (knownYears.length === 1 ? knownYears[0] : '');
      const bucket = yearBuckets.get(year) || [];
      bucket.push(entry);
      yearBuckets.set(year, bucket);
    }

    for (const [year, yearEntries] of yearBuckets) {
      const bestByQuality = new Map<string, { result: TorrentSearchResult; quality: ReturnType<typeof torrentQualityDetails> }>();
      for (const entry of yearEntries) {
        const quality = torrentQualityDetails(entry.result);
        const previous = bestByQuality.get(quality.key);
        if (!previous) {
          // sortedResults is already ordered by the chosen ranking, so the
          // first occurrence is the preferred swarm for this quality.
          bestByQuality.set(quality.key, { result: entry.result, quality });
        }
      }

      const variants = Array.from(bestByQuality.values()).map(value => value.result);
      if (!variants.length) continue;
      // Preserve the current seed/size/time ranking within the quality picker.
      const title = yearEntries[0].identity.title;
      groups.push({
        key: `${normalized}|${year || 'unknown'}`,
        title: year ? `${title} (${year})` : title,
        year,
        variants,
        posterResult: variants.find(item => item.posterUrl || item.mediaTitle) || variants[0],
      });
    }
  }

  return groups;
}

export const TorrentSearchPanel: React.FC<TorrentSearchPanelProps> = ({ onPrepare, onCancelPrepare, onOpenProgress, seedrFiles = [], seedrDeletedFolderIds = [], onPlaySeedrFile }) => {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<TorrentSearchResult[]>([]);
  const [selectedQualityByGroup, setSelectedQualityByGroup] = useState<Record<string, string>>({});
  const [isSearching, setIsSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState('');
  const [catalogueBuildProgress, setCatalogueBuildProgress] = useState('');
  const [movieCatalogue, setMovieCatalogue] = useState<MovieCataloguePage | null>(null);
  const [movieCatalogueSearchText, setMovieCatalogueSearchText] = useState('');
  // Seed count is the default ranking so the strongest swarms appear first.
  // 720p/1080p are mutually exclusive. Size and Time are independent sort toggles.
  const [resolutionFilter, setResolutionFilter] = useState<'720p' | '1080p' | null>(null);
  const [sizeSort, setSizeSort] = useState<'asc' | 'desc' | null>(null);
  const [timeSort, setTimeSort] = useState<'desc' | 'asc' | null>(null);
  const [releaseYearSort, setReleaseYearSort] = useState<'newest' | 'oldest' | null>(null);
  const [showRecentSearches, setShowRecentSearches] = useState(false);
  const [preparingTorrentKey, setPreparingTorrentKey] = useState<string | null>(null);
  const [playingTorrentKey, setPlayingTorrentKey] = useState<string | null>(null);
  const [copiedTorrentKey, setCopiedTorrentKey] = useState<string | null>(null);
  const [prepareWaitTitle, setPrepareWaitTitle] = useState('');
  const [prepareWaitOpen, setPrepareWaitOpen] = useState(false);
  const [prepareError, setPrepareError] = useState('');
  const [fullTorrentTitle, setFullTorrentTitle] = useState<string | null>(null);
  const [fullTorrentTitleKey, setFullTorrentTitleKey] = useState<string | null>(null);
  const [fullTorrentTitleFading, setFullTorrentTitleFading] = useState(false);

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
  const fullTorrentTitleFadeTimerRef = useRef<number | null>(null);
  const fullTorrentTitleHideTimerRef = useRef<number | null>(null);

  // Poster resolution runs independently of torrent search and ranking.
  // Results are queued in seed order, but only two background resolutions run
  // at once so this never becomes a search bottleneck.
  const [posterOverrides, setPosterOverrides] = useState<Record<string, string>>({});
  const posterLoadedRef = useRef(new Set<string>());
  const posterBackgroundQueueRef = useRef<Array<{ key: string; result: TorrentSearchResult }>>([]);
  const posterBackgroundQueuedRef = useRef(new Set<string>());
  const posterBackgroundActiveRef = useRef(0);
  const posterBackgroundGenerationRef = useRef(0);
  const posterBackgroundAttemptsRef = useRef(new Map<string, number>());

  const posterKeyFor = (result: TorrentSearchResult) =>
    String(
      result.guid ||
      result.infoHash ||
      result.magnetUrl ||
      result.downloadUrl ||
      result.title
    );

  const fullTorrentTitleFor = (result: TorrentSearchResult) => {
    // Indexer search pages can truncate the displayed title, while the magnet's
    // dn parameter normally carries the actual full torrent name.
    const magnet = String(result.magnetUrl || result.downloadUrl || '').trim();
    if (magnet) {
      try {
        const query = magnet.includes('?') ? magnet.slice(magnet.indexOf('?') + 1) : '';
        const params = new URLSearchParams(query);
        const displayName = params.get('dn')?.trim();
        if (displayName) return displayName;
      } catch {
        // Fall back to the API title below.
      }
    }

    return String(result.title || result.mediaTitle || '').trim();
  };

  const rawPosterUrlFor = (result: TorrentSearchResult) => {
    const raw = String(result.posterUrl || '').trim();
    if (!raw) return '';
    return raw.startsWith('/') ? API_BASE + raw : raw;
  };

  const posterResolveCandidateUrlsFor = (result: TorrentSearchResult) => {
    const candidates: string[] = [];
    const seen = new Set<string>();

    const addCandidate = (title: string, year = '') => {
      const clean = String(title || '')
        .replace(/[._]+/g, ' ')
        .replace(/\s+/g, ' ')
        .replace(/^[-._\s]+|[-._\s]+$/g, '')
        .trim();
      if (!clean) return;

      const yearMatch = clean.match(/\b((?:19|20)\d{2})\b/);
      const inferredYear = year || yearMatch?.[1] || '';
      const titleOnly = clean.replace(/\s*\b((?:19|20)\d{2})\b.*$/i, '').trim() || clean;

      const params = new URLSearchParams({ title: titleOnly });
      if (inferredYear) params.set('year', inferredYear);
      const url = API_BASE + '/api/poster/resolve?' + params.toString();
      if (!seen.has(url)) {
        seen.add(url);
        candidates.push(url);
      }
    };

    // Ask the existing poster resolver first. This uses the same lookup and
    // cache as normal image rendering, so successful work is shared.
    const rawUrl = rawPosterUrlFor(result);
    if (rawUrl) {
      addCandidate(
        rawUrl.split('?title=')[1]?.split('&')[0]
          ? decodeURIComponent(rawUrl.split('?title=')[1].split('&')[0].replace(/\+/g, ' '))
          : String(result.mediaTitle || result.title || ''),
        String(result.year || '')
      );
    }

    const explicitYear = result.year ? String(result.year) : '';
    const mediaTitle = String(result.mediaTitle || '').trim();
    const rawTitle = String(result.title || '').trim();
    const baseTitle = mediaTitle || rawTitle;

    addCandidate(mediaTitle, explicitYear);

    const cleaned = baseTitle
      .replace(/[._]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();

    const yearMatch = cleaned.match(/\b((?:19|20)\d{2})\b/);
    const year = explicitYear || yearMatch?.[1] || '';

    if (!mediaTitle && yearMatch?.index != null) {
      addCandidate(cleaned.slice(0, yearMatch.index), year);
    }

    const releaseCut = cleaned.split(
      /\b(?:2160p|1440p|1080p|720p|576p|480p|4k|8k|web[- ]?dl|web[- ]?rip|webrip|bluray|brrip|hdrip|dvdrip|cam|hdcam|x264|x265|h264|h265|hevc|aac|ddp|atmos|proper|repack|remastered|extended|unrated|directors?\s+cut)\b/i
    )[0].trim();
    if (releaseCut) addCandidate(releaseCut, year);

    const softClean = cleaned
      .replace(
        /\b(?:hindi|tamil|telugu|malayalam|kannada|bengali|marathi|punjabi|gujarati|urdu|dual\s+audio|multi\s+audio|dubbed|dub|multi|proper|repack|remastered|extended|unrated|imax|hdr10\+?|dolby\s+vision)\b/gi,
        ' '
      )
      .replace(/\s+/g, ' ')
      .trim();
    if (softClean) addCandidate(softClean, year);

    const aliasPairs: Array<[RegExp, string]> = [
      [/\bspiderman\b/gi, 'Spider Man'],
      [/\bspider\s+man\b/gi, 'Spiderman'],
      [/\bantman\b/gi, 'Ant Man'],
      [/\bant\s+man\b/gi, 'Antman'],
      [/\bironman\b/gi, 'Iron Man'],
      [/\biron\s+man\b/gi, 'Ironman'],
      [/\bblackpanther\b/gi, 'Black Panther'],
      [/\bblack\s+panther\b/gi, 'Blackpanther'],
      [/\bdoctorstrange\b/gi, 'Doctor Strange'],
      [/\bdoctor\s+strange\b/gi, 'Doctorstrange'],
      [/\bcaptainamerica\b/gi, 'Captain America'],
      [/\bcaptain\s+america\b/gi, 'Captainamerica'],
      [/\bguardiansofthegalaxy\b/gi, 'Guardians of the Galaxy'],
    ];

    for (const [pattern, replacement] of aliasPairs) {
      if (pattern.test(cleaned)) {
        addCandidate(cleaned.replace(pattern, replacement), year);
      }
    }

    return candidates;
  };

  const resolvePosterInBackground = async (url: string, generation: number) => {
    if (generation !== posterBackgroundGenerationRef.current) return '';

    try {
      const response = await fetch(url, {
        method: 'GET',
        credentials: 'include',
        cache: 'force-cache'
      });

      if (!response.ok) return '';

      const data = await response.json().catch(() => null);
      const resolved = String(data?.url || '').trim();
      return resolved;
    } catch {
      return '';
    }
  };

  const startPosterBackgroundWorkers = () => {
    const generation = posterBackgroundGenerationRef.current;

    while (
      posterBackgroundActiveRef.current < 2 &&
      posterBackgroundQueueRef.current.length > 0
    ) {
      const job = posterBackgroundQueueRef.current.shift();
      if (!job) break;

      posterBackgroundQueuedRef.current.delete(job.key);
      posterBackgroundActiveRef.current += 1;

      void (async () => {
        try {
          if (generation !== posterBackgroundGenerationRef.current) return;

          const key = job.key;

          // A visible/loaded poster wins immediately; don't spend background
          // work resolving something the browser already has.
          if (posterLoadedRef.current.has(key)) return;

          const candidates = posterResolveCandidateUrlsFor(job.result);
          for (const candidate of candidates) {
            if (generation !== posterBackgroundGenerationRef.current) return;

            const resolved = await resolvePosterInBackground(candidate, generation);
            if (resolved) {
              setPosterOverrides(prev => (
                prev[key] === resolved ? prev : { ...prev, [key]: resolved }
              ));
              return;
            }

            await new Promise(resolve => window.setTimeout(resolve, 250));
          }

          // One delayed retry gives transient provider failures another chance.
          const attempts = posterBackgroundAttemptsRef.current.get(key) || 0;
          if (attempts < 1 && generation === posterBackgroundGenerationRef.current) {
            posterBackgroundAttemptsRef.current.set(key, attempts + 1);
            window.setTimeout(() => {
              if (generation !== posterBackgroundGenerationRef.current) return;
              if (posterLoadedRef.current.has(key)) return;
              if (posterBackgroundQueuedRef.current.has(key)) return;

              posterBackgroundQueuedRef.current.add(key);
              posterBackgroundQueueRef.current.push(job);
              startPosterBackgroundWorkers();
            }, 45000);
          }
        } finally {
          posterBackgroundActiveRef.current = Math.max(
            0,
            posterBackgroundActiveRef.current - 1
          );

          if (generation === posterBackgroundGenerationRef.current) {
            startPosterBackgroundWorkers();
          }
        }
      })();
    }
  };

  const startPosterBackgroundSearch = (items: TorrentSearchResult[]) => {
    posterBackgroundGenerationRef.current += 1;
    posterBackgroundQueueRef.current = [];
    posterBackgroundQueuedRef.current.clear();
    posterBackgroundAttemptsRef.current.clear();
    posterLoadedRef.current.clear();

    const generation = posterBackgroundGenerationRef.current;

    for (const result of items) {
      const key = posterKeyFor(result);
      if (!key || posterBackgroundQueuedRef.current.has(key)) continue;

      posterBackgroundQueuedRef.current.add(key);
      posterBackgroundQueueRef.current.push({ key, result });
    }

    // Start immediately after results arrive, but never await this work.
    if (generation === posterBackgroundGenerationRef.current) {
      startPosterBackgroundWorkers();
    }
  };

  const posterUrlFor = (result: TorrentSearchResult) =>
    posterOverrides[posterKeyFor(result)] || rawPosterUrlFor(result);

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

    // Only auto-associate an existing Seedr file when the torrent title matches
    // the actual Seedr folder/file name exactly. The previous fuzzy fallback
    // could make one downloaded torrent appear as "Play" on several different
    // search results for the same movie (e.g. different qualities/releases).
    return seedrFiles.filter(file => {
      const folderPath = String(file.folderPath || '');
      const folderName = folderPath.split('/').filter(Boolean).pop() || '';
      const fileName = normalizeMatchText(file.name);
      const folder = normalizeMatchText(folderName);

      return folder === title || fileName === title;
    });
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

  const showFullTorrentTitleBubble = (result: TorrentSearchResult) => {
    if (fullTorrentTitleFadeTimerRef.current !== null) {
      window.clearTimeout(fullTorrentTitleFadeTimerRef.current);
    }
    if (fullTorrentTitleHideTimerRef.current !== null) {
      window.clearTimeout(fullTorrentTitleHideTimerRef.current);
    }

    const key = posterKeyFor(result);
    setFullTorrentTitleKey(key);
    setFullTorrentTitle(String(result.title || result.mediaTitle || '').trim());
    setFullTorrentTitleFading(false);

    fullTorrentTitleFadeTimerRef.current = window.setTimeout(() => {
      setFullTorrentTitleFading(true);
    }, 5550);

    fullTorrentTitleHideTimerRef.current = window.setTimeout(() => {
      setFullTorrentTitle(null);
      setFullTorrentTitleKey(null);
      setFullTorrentTitleFading(false);
    }, 6000);
  };

  useEffect(() => {
    return () => {
      if (fullTorrentTitleFadeTimerRef.current !== null) {
        window.clearTimeout(fullTorrentTitleFadeTimerRef.current);
      }
      if (fullTorrentTitleHideTimerRef.current !== null) {
        window.clearTimeout(fullTorrentTitleHideTimerRef.current);
      }
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








  const runSearch = async (event?: React.FormEvent, searchOverride?: string, searchMode?: 'marvel' | 'dc-live-action' | 'dc-animated' | 'latest-hollywood' | 'latest-bollywood') => {
    event?.preventDefault();

    const trimmed = (searchOverride ?? query).trim();
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
      setCatalogueBuildProgress('');
      setMovieCatalogue(null);
      setShowRecentSearches(false);
      setError('');
      setResults([]);
      setSelectedQualityByGroup({});
      setSearched(false);
      setPosterOverrides({});
      posterLoadedRef.current.clear();
      posterBackgroundQueueRef.current = [];
      posterBackgroundQueuedRef.current.clear();
      posterBackgroundAttemptsRef.current.clear();
      posterBackgroundGenerationRef.current += 1;
      saveRecentSearch(trimmed);
      setCatalogueBuildProgress('');

      let data: TorrentSearchResult[];
      if (searchMode) {
        const labels: Record<string, string> = {
          marvel: 'Marvel', 'dc-live-action': 'DC live-action', 'dc-animated': 'DC animated',
          'latest-hollywood': 'Latest Hollywood', 'latest-bollywood': 'Latest Bollywood'
        };
        let catalogue = searchMode === 'marvel'
          ? await api.getMarvelCatalogue(controller.signal)
          : await api.getCatalogue(searchMode, controller.signal);
        while (catalogue.status === 'building' || catalogue.status === 'idle') {
          if (generation !== searchGenerationRef.current) return;
          setCatalogueBuildProgress(
            'Building shared ' + (labels[searchMode] || 'movie') + ' catalogue: ' +
            catalogue.completed + ' of ' + catalogue.total +
            ' searches completed. Results will be cached for future visitors.'
          );
          await new Promise<void>(resolve => window.setTimeout(resolve, 2200));
          catalogue = searchMode === 'marvel'
            ? await api.getMarvelCatalogue(controller.signal)
            : await api.getCatalogue(searchMode, controller.signal);
        }
        if (catalogue.status !== 'ready') {
          throw new Error(catalogue.error || ('Could not load the ' + (labels[searchMode] || 'movie') + ' catalogue.'));
        }
        data = catalogue.results;
        setCatalogueBuildProgress(catalogue.refreshing ? 'Showing cached results while the monthly refresh runs in the background.' : '');
      } else {
        data = await api.searchTorrents(trimmed, 50, controller.signal);
      }

      // The backend owns low-result TV/season fallback. Keeping that logic
      // server-side avoids launching duplicate season searches from the browser.

      // Never let an older request overwrite a newer search. This matters
      // when a slow 1337x fallback finishes after a later click.
      if (generation !== searchGenerationRef.current) return;

      setResults(data);
      setSearched(true);
      startPosterBackgroundSearch(data);

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
      setCatalogueBuildProgress('');
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

  const openMovieCatalogue = async (key: MovieCatalogueKey, searchText: string, page = 1) => {
    const generation = ++searchGenerationRef.current;
    searchRequestRef.current?.abort();
    const controller = new AbortController();
    searchRequestRef.current = controller;

    try {
      setIsSearching(true);
      setCatalogueBuildProgress('Loading the ' + searchText.replace(/Movies?$/i, '').trim() + ' movie catalogue…');
      setMovieCatalogueSearchText(searchText);
      setShowRecentSearches(false);
      setQuery(searchText);
      setError('');
      setResults([]);
      setSelectedQualityByGroup({});
      setSearched(false);
      setPosterOverrides({});
      posterLoadedRef.current.clear();
      posterBackgroundQueueRef.current = [];
      posterBackgroundQueuedRef.current.clear();
      posterBackgroundAttemptsRef.current.clear();
      posterBackgroundGenerationRef.current += 1;

      const data = await api.getMovieCatalogue(key, page, controller.signal);
      if (generation !== searchGenerationRef.current) return;
      setMovieCatalogue(data);
      setCatalogueBuildProgress('');
    } catch (err: any) {
      if (generation !== searchGenerationRef.current) return;
      if (err?.name === 'AbortError') return;

      const message = String(err?.message || 'Movie catalogue failed.');
      const legacyFallbackKey: Record<MovieCatalogueKey, 'marvel' | 'dc-live-action' | 'dc-animated' | 'latest-hollywood' | 'latest-bollywood'> = {
        marvel: 'marvel',
        'dc-live-action': 'dc-live-action',
        'dc-animated': 'dc-animated',
        'latest-hollywood': 'latest-hollywood',
        'latest-bollywood': 'latest-bollywood',
        'popular-hollywood': 'latest-hollywood',
        'trending-hollywood': 'latest-hollywood',
        'popular-bollywood': 'latest-bollywood',
        'trending-bollywood': 'latest-bollywood',
      };
      const fallbackKey = legacyFallbackKey[key];
      if (fallbackKey) {
        setMovieCatalogue(null);
        await runSearch(undefined, searchText, fallbackKey);
        const categoryFallback = fallbackKey === 'latest-hollywood' && key !== 'latest-hollywood'
          ? 'Latest Hollywood'
          : fallbackKey === 'latest-bollywood' && key !== 'latest-bollywood'
            ? 'Latest Bollywood'
            : searchText.replace(/Movies?$/i, '').trim();
        const reason = message.includes('TMDB API credentials are not configured')
          ? 'TMDB credentials are not configured.'
          : message;
        setError(
          reason + ' Falling back to the existing ' + categoryFallback +
          ' torrent catalogue where cached results are available. Popular and Trending shortcuts use Latest results during the outage.'
        );
      } else {
        setMovieCatalogue(null);
        setError(message);
      }
    } finally {
      if (generation === searchGenerationRef.current) {
        setIsSearching(false);
        setCatalogueBuildProgress('');
        if (searchRequestRef.current === controller) {
          searchRequestRef.current = null;
        }
      }
    }
  };

  const sortedResults = useMemo(() => {
    // Home intentionally allows up to 5 GB. new-test remains the 2 GB variant.
    const maxSeedrFriendlySize = 5 * 1024 * 1024 * 1024;
    // Exclude theatrical/unfinished releases regardless of provider. Keep the
    // tokens bounded so words such as "timestamp" are not mistaken for TS.
    const lowQualityRelease = /(?:^|[\s._()[\]-])(?:cam(?:rip)?|hdcam|hd[ ._-]?cam|telesync|tele[ ._-]?sync|ts[ ._-]?(?:md|ac3|hd)?|telecine|dvdscr|dvd[ ._-]?scr|screener|workprint)(?:$|[\s._()[\]-])/i;
    const sorted = results.filter(result => {
      const size = Number(result.size) || 0;
      const seeders = Number(result.seeders);
      if (!Number.isFinite(seeders) || seeders <= 0) return false;
      if (size > maxSeedrFriendlySize) return false;
      const releaseText = `${String(result.title || '')} ${String(result.quality || '')}`;
      if (lowQualityRelease.test(releaseText)) return false;
      if (resolutionFilter) {
        const quality = torrentQualityDetails(result).label;
        const pattern = resolutionFilter === '720p' ? /(?:^|[^0-9])720p(?:[^0-9]|$)/i : /(?:^|[^0-9])1080p(?:[^0-9]|$)/i;
        if (!pattern.test(quality)) return false;
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
      // Keep the backend's relevance-first ranking by default. Re-sorting here
      // by quality/seeders would undo its title-match scoring and let popular
      // but less relevant releases appear above better matches.
      return 0;
    });
    return sorted;
  }, [results, resolutionFilter, sizeSort, timeSort]);

  const groupedResults = useMemo(() => {
    const groups = groupTorrentResults(sortedResults);
    if (!releaseYearSort) return groups;

    // Sort whole movie groups, never individual torrent variants. The year is
    // the movie's release year (catalogue metadata), not the torrent upload date.
    return [...groups].sort((a, b) => {
      const yearA = Number.parseInt(a.year, 10) || 0;
      const yearB = Number.parseInt(b.year, 10) || 0;
      // Keep entries with unknown release years at the bottom in either mode.
      if (!yearA && !yearB) return 0;
      if (!yearA) return 1;
      if (!yearB) return -1;
      return releaseYearSort === 'newest' ? yearB - yearA : yearA - yearB;
    });
  }, [sortedResults, releaseYearSort]);

  const extractedQuality = (result: TorrentSearchResult) => {
    const details = torrentQualityDetails(result);
    return details.label === 'Other release' ? '' : details.label;
  };

  return (
    <div className="space-y-2.5 sm:space-y-4">
      <div className="p-2.5 sm:p-5 rounded-xl sm:rounded-2xl bg-slate-900 border border-slate-800">
        <div className="flex flex-col gap-1">
          <h2 className="text-base font-bold text-slate-100 flex items-center gap-2">
            <Search className="w-5 h-5 text-cyan-400" />
            Search Torrents
          </h2>
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
                  setMovieCatalogue(null);
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

        <div className="mt-2 flex flex-wrap gap-2" aria-label="Movie catalogues">
          {([
            ['marvel', '🦸 Marvel Movies', 'Marvel Movies'],
            ['dc-live-action', '🦇 DC Live-Action', 'DC Live-Action Movies'],
            ['dc-animated', '🎞️ DC Animated', 'DC Animated Movies'],
            ['latest-hollywood', '🎬 Latest Hollywood', 'Latest Hollywood Movies'],
            ['popular-hollywood', '⭐ Popular Hollywood', 'Popular Hollywood Movies'],
            ['trending-hollywood', '🔥 Trending Hollywood', 'Trending Hollywood Movies'],
            ['latest-bollywood', '🎥 Latest Bollywood', 'Latest Bollywood Movies'],
            ['popular-bollywood', '⭐ Popular Bollywood', 'Popular Bollywood Movies'],
            ['trending-bollywood', '🔥 Trending Bollywood', 'Trending Bollywood Movies'],
          ] as const).map(([key, label, searchText]) => (
            <button key={key} type="button" onClick={() => {
              setResolutionFilter(null);
              setSizeSort(null);
              setTimeSort(null);
              setReleaseYearSort(null);
              void openMovieCatalogue(key, searchText, 1);
            }} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:border-cyan-500 hover:text-cyan-300">{label}</button>
          ))}
        </div>
      </div>

      {isSearching && (
        <div className="p-5 sm:p-7 rounded-xl sm:rounded-2xl bg-slate-900 border border-slate-800 flex flex-col items-center justify-center gap-3">
          <Loader2 className="w-8 h-8 text-cyan-400 animate-spin" />
          <div className="text-sm font-semibold text-slate-200">{catalogueBuildProgress ? 'Loading movie catalogue...' : 'Searching torrents...'}</div>
          <div className="text-xs text-slate-500 text-center">{catalogueBuildProgress || 'Checking the fastest media sources and waiting for results.'}</div>
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

      {movieCatalogue && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2 px-1">
            <div className="text-xs text-slate-400">
              {movieCatalogue.catalogue.startsWith('trending-')
                ? movieCatalogue.results.length + ' trending titles on this page · Page ' + movieCatalogue.page + ' of ' + movieCatalogue.totalPages
                : movieCatalogue.totalResults.toLocaleString() + ' movies · Page ' + movieCatalogue.page + ' of ' + movieCatalogue.totalPages.toLocaleString()
              } · Metadata by {movieCatalogue.provider}
            </div>
            <div className="text-[11px] text-slate-500">Select a movie to search torrent providers</div>
          </div>

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 lg:gap-4">
            {movieCatalogue.results.map(movie => (
              <article key={String(movie.id)} className="min-w-0 overflow-hidden rounded-xl border border-slate-800 bg-slate-900 flex flex-col">
                <div className="relative aspect-[2/3] bg-slate-950">
                  {movie.posterUrl ? (
                    <img
                      src={movie.posterUrl}
                      alt={movie.title}
                      loading="lazy"
                      className="h-full w-full object-cover"
                      onError={(event) => { event.currentTarget.style.display = 'none'; }}
                    />
                  ) : (
                    <div className="absolute inset-0 flex items-center justify-center text-slate-600">
                      <Film className="h-8 w-8" />
                    </div>
                  )}
                  {movie.rating != null && movie.rating > 0 && (
                    <span className="absolute left-2 top-2 rounded-md bg-slate-950/90 px-2 py-1 text-[10px] font-bold text-emerald-300">
                      ★ {movie.rating.toFixed(1)}
                    </span>
                  )}
                </div>
                <div className="flex flex-1 flex-col gap-2 p-2.5">
                  <div>
                    <h3 className="text-sm font-bold leading-snug text-slate-100">{movie.title}</h3>
                    <p className="mt-0.5 text-xs text-slate-500">{movie.year || 'Release year unknown'}</p>
                  </div>
                  {movie.overview && (
                    <p className="line-clamp-3 text-[11px] leading-relaxed text-slate-400">{movie.overview}</p>
                  )}
                  <button
                    type="button"
                    disabled={isSearching}
                    onClick={() => {
                      const titleQuery = movie.title + (movie.year ? ' ' + movie.year : '');
                      setMovieCatalogue(null);
                      setQuery(titleQuery);
                      void runSearch(undefined, titleQuery);
                    }}
                    className="mt-auto w-full rounded-lg bg-cyan-500 px-3 py-2 text-xs font-bold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
                  >
                    <Search className="mr-1.5 inline h-3.5 w-3.5" />
                    Find torrents
                  </button>
                </div>
              </article>
            ))}
          </div>

          <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900 px-3 py-2">
            <button
              type="button"
              disabled={isSearching || movieCatalogue.page <= 1}
              onClick={() => void openMovieCatalogue(movieCatalogue.catalogue, movieCatalogueSearchText, movieCatalogue.page - 1)}
              className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs font-semibold text-slate-300 hover:border-cyan-500 disabled:opacity-40"
            >
              Previous
            </button>
            <span className="text-xs text-slate-500">Browse all pages to explore older releases too</span>
            <button
              type="button"
              disabled={isSearching || movieCatalogue.page >= movieCatalogue.totalPages}
              onClick={() => void openMovieCatalogue(movieCatalogue.catalogue, movieCatalogueSearchText, movieCatalogue.page + 1)}
              className="rounded-lg bg-cyan-500 px-3 py-2 text-xs font-bold text-slate-950 hover:bg-cyan-400 disabled:opacity-40"
            >
              Next
            </button>
          </div>

          <div className="flex flex-col items-start gap-2 rounded-xl border border-slate-800 bg-slate-900/70 px-3 py-3 sm:flex-row sm:items-center">
            <a href="https://www.themoviedb.org/" target="_blank" rel="noreferrer" className="inline-flex shrink-0 items-center gap-2">
              <img
                src="https://www.themoviedb.org/assets/2/v4/logos/v2/blue_square_1-5bdc75aaebeb75dc7ae79426ddd9be3b2be1e342510f8202baf6bffa71d7f5c4.svg"
                alt="The Movie Database (TMDB)"
                className="h-7 w-7 object-contain"
              />
              <span className="text-xs font-bold text-slate-300">The Movie Database (TMDB)</span>
            </a>
            <p className="text-[10px] leading-relaxed text-slate-500">
              This product uses the TMDB API but is not endorsed or certified by TMDB.
            </p>
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
              <button type="button" onClick={() => { setReleaseYearSort(null); setSizeSort(current => current === null ? 'asc' : current === 'asc' ? 'desc' : null); }} className={sizeSort ? 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950 shadow-[0_0_14px_rgba(34,211,238,0.45)]' : 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={Boolean(sizeSort)} title="Sort by torrent size">Size {sizeSort === 'asc' ? '↑' : sizeSort === 'desc' ? '↓' : ''}</button>
              <button type="button" onClick={() => { setReleaseYearSort(null); setTimeSort(current => current === null ? 'desc' : current === 'desc' ? 'asc' : null); }} className={timeSort ? 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950 shadow-[0_0_14px_rgba(34,211,238,0.45)]' : 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={Boolean(timeSort)} title="Sort by torrent upload date">Time {timeSort === 'desc' ? '↓' : timeSort === 'asc' ? '↑' : ''}</button>
              <button type="button" onClick={() => { setSizeSort(null); setTimeSort(null); setReleaseYearSort(current => current === null ? 'newest' : current === 'newest' ? 'oldest' : null); }} className={releaseYearSort ? 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-cyan-500 text-slate-950 shadow-[0_0_14px_rgba(34,211,238,0.45)]' : 'shrink-0 rounded-lg px-3 py-2 text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800 hover:text-slate-200'} aria-pressed={Boolean(releaseYearSort)} title="Sort movie cards by release year: newest first, oldest first, then default ranking">{releaseYearSort === 'newest' ? 'Year ↓ Newest' : releaseYearSort === 'oldest' ? 'Year ↑ Oldest' : 'Year'}</button>
            </div>
          </div>

          <div className="flex items-center justify-between gap-2 px-1">
            <div className="text-xs text-slate-400">
              {groupedResults.length} titles · {sortedResults.length} torrent options
            </div>
          </div>

          <div
            className="grid grid-cols-2 gap-2 sm:block sm:rounded-2xl sm:border sm:border-slate-800 sm:overflow-hidden sm:bg-slate-900 sm:divide-y sm:divide-slate-800/80 lg:grid lg:grid-cols-6 lg:gap-[22px] lg:w-full lg:max-w-none lg:mx-0 lg:p-0 lg:border-0 lg:bg-transparent lg:divide-y-0 lg:overflow-visible"
          >
            {groupedResults.map((group, index) => {
              const selectedKey = selectedQualityByGroup[group.key];
              const result = group.variants.find(variant => torrentResultKey(variant) === selectedKey) || group.variants[0];
              const posterResult = group.posterResult;
              return (
              <div
                key={group.key || (group.title + '-' + index)}
                className={[
                  'relative min-w-0 rounded-xl border border-slate-800 bg-slate-900 p-2 hover:bg-slate-800/80 transition sm:rounded-none sm:border-0 sm:bg-transparent sm:p-2 sm:px-4 sm:py-4 lg:flex lg:flex-col lg:self-start lg:h-fit lg:rounded-[14px] lg:border lg:border-slate-800 lg:bg-slate-900 lg:p-0 lg:hover:-translate-y-1 lg:hover:border-slate-700',
                  fullTorrentTitleKey === posterKeyFor(result) ? 'z-50' : 'z-0'
                ].join(' ')}
              >
                <div className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3 lg:flex-col lg:items-stretch lg:gap-0">
                  <div className="min-w-0 flex-1 lg:w-full">
                    <div className="flex flex-col sm:flex-row items-stretch sm:items-start gap-2 sm:gap-3 lg:flex-col lg:gap-0">
                      <div className="relative w-full sm:w-24 shrink-0 aspect-[2/3] rounded-lg overflow-hidden border border-slate-800 bg-slate-950 shadow-md lg:w-full lg:rounded-none lg:border-0 lg:shadow-none">
                        {posterUrlFor(posterResult) ? (
                          <>
                            <img
                              src={posterUrlFor(posterResult)}
                              alt={group.title}
                              loading="lazy"
                              className="w-full h-full object-cover"
                              onLoad={(event) => {
                                const key = posterKeyFor(posterResult);
                                posterLoadedRef.current.add(key);
                                event.currentTarget.style.display = '';
                                event.currentTarget.parentElement?.querySelector('[data-poster-placeholder="true"]')?.classList.add('hidden');
                              }}
                              onError={(event) => {
                                const key = posterKeyFor(posterResult);
                                posterLoadedRef.current.delete(key);
                                event.currentTarget.style.display = 'none';
                                event.currentTarget.parentElement?.querySelector('[data-poster-placeholder="true"]')?.classList.remove('hidden');

                                if (
                                  !posterBackgroundQueuedRef.current.has(key) &&
                                  !posterOverrides[key]
                                ) {
                                  posterBackgroundQueuedRef.current.add(key);
                                  posterBackgroundQueueRef.current.unshift({ key, result: posterResult });
                                  startPosterBackgroundWorkers();
                                }
                              }}
                            />
                            <div data-poster-placeholder="true" className="hidden absolute inset-0 flex items-center justify-center bg-slate-950 text-slate-500">
                              <Film className="w-7 h-7" />
                            </div>
                          </>
                        ) : (
                          <div className="absolute inset-0 flex items-center justify-center bg-slate-950 text-slate-700">
                            <Film className="w-7 h-7" />
                          </div>
                        )}

                        {result.seeders > 0 && (
                          <div className="absolute left-2 top-2 inline-flex items-center gap-1 rounded-md bg-black/80 px-2 py-1 text-[10px] font-bold text-emerald-400">
                            <Users className="w-3 h-3" />
                            {result.seeders}
                          </div>
                        )}

                        {result.indexer && (
                          <div
                            className="absolute right-2 top-2 max-w-[70%] truncate rounded-md bg-black/75 px-2 py-1 text-[10px] font-medium text-slate-300"
                            title={result.indexer}
                          >
                            {result.indexer}
                          </div>
                        )}

                        {extractedQuality(result) && (
                          <div className="absolute left-2 bottom-2 rounded-md border border-cyan-400/30 bg-slate-950/85 px-2 py-1 text-[10px] font-bold text-cyan-300">
                            {extractedQuality(result)}
                          </div>
                        )}
                      </div>

                      <div className="min-w-0 flex-1 lg:w-full lg:flex-none lg:p-3">
                        <div className="flex items-start gap-2">
                          <div className="hidden sm:flex lg:hidden p-1.5 rounded-lg bg-cyan-500/10 border border-cyan-500/20 shrink-0">
                            <Database className="w-3.5 h-3.5 text-cyan-400" />
                          </div>
                          <div className="relative min-w-0 flex-1 z-10">
                            <button
                              type="button"
                              onClick={() => {
                                const fullTitle = fullTorrentTitleFor(result);
                                showFullTorrentTitleBubble({
                                  ...result,
                                  title: fullTitle || result.title
                                });
                              }}
                              className="block w-full text-left text-[11px] leading-4 sm:text-sm font-semibold text-slate-100 line-clamp-2 hover:text-cyan-300 transition cursor-pointer"
                              title="Click to view full torrent name"
                              aria-label="View full torrent name"
                            >
                              {group.title}
                            </button>

                            <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[10px] sm:text-xs">
                              <span className="font-semibold text-emerald-400">▲ {Number(result.seeders) || 0} seeders</span>
                              <span className="font-semibold text-amber-400">▼ {Number(result.leechers) || 0} peers</span>
                              {group.year && <span className="text-slate-500">{group.year}</span>}
                            </div>

                            {group.variants.length > 1 && (
                              <select
                                aria-label={`Choose quality for ${group.title}`}
                                value={torrentResultKey(result)}
                                onChange={(event) => setSelectedQualityByGroup(previous => ({
                                  ...previous,
                                  [group.key]: event.target.value
                                }))}
                                className="mt-2 w-full min-w-0 rounded-lg border border-slate-800 bg-slate-950 px-2 py-2 text-[10px] sm:text-xs font-medium text-slate-200 outline-none focus:border-cyan-500"
                                title="Choose quality and torrent source"
                              >
                                {group.variants.map(variant => {
                                  const quality = torrentQualityDetails(variant);
                                  const provider = String(variant.indexer || '').trim();
                                  const size = formatBytes(Number(variant.size) || 0);
                                  const seeds = Number(variant.seeders) || 0;
                                  return (
                                    <option key={torrentResultKey(variant)} value={torrentResultKey(variant)}>
                                      {quality.label} · {size} · ▲{seeds}{provider ? ` · ${provider}` : ''}
                                    </option>
                                  );
                                })}
                              </select>
                            )}

                            {fullTorrentTitleKey === posterKeyFor(result) && fullTorrentTitle && (
                              <div
                                role="status"
                                aria-live="polite"
                                className={[
                                  'absolute bottom-full left-0 mb-1.5 z-50 w-max max-w-[min(460px,calc(100vw-24px))] rounded-xl border border-cyan-400/25 bg-slate-950/95 px-3 py-2 text-[11px] leading-4 font-medium text-slate-100 shadow-xl transition-all duration-[450ms] ease-out whitespace-normal break-all',
                                  fullTorrentTitleFading
                                    ? 'translate-y-1 opacity-0'
                                    : 'translate-y-0 opacity-100'
                                ].join(' ')}
                              >
                                {fullTorrentTitle}
                                <span className="absolute left-4 top-full h-2.5 w-2.5 -translate-y-1 rotate-45 border-r border-b border-cyan-400/25 bg-slate-950/95" />
                              </div>
                            )}
                          </div>
                        </div>
                        <div className="mt-2 flex items-center justify-between gap-2">
                          <span className="shrink-0 font-mono text-[10px] sm:text-[11px] text-slate-300">
                            {formatBytes(result.size)}
                          </span>

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
                          <div className="min-w-0 max-w-full flex flex-wrap items-center justify-end gap-1.5">
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
                              className="shrink-0 px-2 py-1.5 rounded-lg bg-emerald-400 text-slate-950 font-bold text-xs hover:bg-emerald-300 transition flex items-center gap-1 disabled:opacity-70 disabled:cursor-wait whitespace-nowrap"
                              title={isPlaying ? "Opening stream…" : "Play from Seedr"}
                            >
                              {isPlaying
                                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                : <Play className="w-3.5 h-3.5" />}
                            </button>

                            <button
                              type="button"
                              onClick={() => api.openSeedrFileDownload(primaryFile.id, primaryFile.name)}
                              className="shrink-0 p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white transition"
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
                              className="shrink-0 p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-cyan-400 transition disabled:opacity-60"
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
                        <div className="min-w-0 max-w-full flex flex-wrap items-center justify-end gap-1.5">
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
                                const message = String(error?.message || 'Could not prepare this torrent.');
                                // Replacing an in-progress Seedr torrent intentionally rejects
                                // the previous waiter; don't show that as a failure for the new pick.
                                if (!message.toLowerCase().includes('cancelled by another selection')) {
                                  setPrepareError(message);
                                }
                              } finally {
                                window.clearTimeout(longWaitTimer);
                                setPreparingTorrentKey(current => current === torrentKey ? null : current);
                              }
                            }}
                            className="shrink-0 px-3 py-1.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 disabled:opacity-60 disabled:cursor-not-allowed text-slate-950 text-[11px] font-bold transition"
                          >
                            {isPreparing ? 'Preparing…' : 'Prepare'}
                          </button>
                        </div>
                      );
                    })()}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
              );
            })}
          </div>
        </div>
      )}

      {!movieCatalogue && !isSearching && searched && groupedResults.length === 0 && !error && (
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
