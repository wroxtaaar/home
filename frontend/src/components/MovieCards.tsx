import React from 'react';
import { Check, Copy, Database, Download, Film, Loader2, Play, Users, X } from 'lucide-react';
import type { ImdbRatingResult } from '../api/client.ts';
import { MovieCatalogueCard } from './MovieCatalogueCard.tsx';

/**
 * The two movie cards used by EVERY movie section (Marvel, DC, DC Animated,
 * Classics, Bollywood, Hollywood, TMDB discovery and normal search results):
 *
 *   1. MovieCompactCard  - poster, IMDb rating, title, synopsis, streaming services.
 *      Clicking the poster opens the prepare card.
 *   2. MoviePrepareCard  - torrent details: quality picker, seeders, source,
 *      Direct Stream + Prepare, and Play / Download / Copy once prepared.
 *
 * Web series cards are intentionally separate. These components are purely
 * presentational: callers supply data and handlers, so a design or behaviour
 * change made here applies to every movie card at once.
 */

export type MovieCardImdb = Pick<ImdbRatingResult, 'imdbRating' | 'imdbVotes' | 'imdbId' | 'imdbUrl'> | null | undefined;

export interface MovieCardProvider {
  key: string;
  name: string;
  webUrl?: string | null;
}

/**
 * Staged poster loading shared by every movie card: cards first show the small (w342)
 * poster; the large (w780) version is only requested once the page's streaming, IMDb
 * and other info has settled, so the user sees complete cards before heavy downloads.
 */
let highQualityPostersReady = false;
const pendingHighQualityPosters = new Set<HTMLImageElement>();

function upgradeToHighQualityPoster(image: HTMLImageElement): void {
  if (!image.isConnected || image.dataset.highQualityTried) return;
  const current = image.currentSrc || image.src;
  if (!current.includes('/t/p/w342/')) return;
  image.dataset.highQualityTried = 'true';
  image.src = current.replace('/t/p/w342/', '/t/p/w780/');
}

export function requestHighQualityPoster(image: HTMLImageElement): void {
  if (highQualityPostersReady) upgradeToHighQualityPoster(image);
  else pendingHighQualityPosters.add(image);
}

/** Call with false while a page is still loading info, true once it has settled. */
export function setHighQualityPostersReady(ready: boolean): void {
  highQualityPostersReady = ready;
  if (!ready) return;
  const images = [...pendingHighQualityPosters];
  pendingHighQualityPosters.clear();
  // Stagger so the upgrades don't all hit the network at once.
  images.forEach((image, index) => window.setTimeout(() => upgradeToHighQualityPoster(image), index * 120));
}

type PosterEventHandler = (event: React.SyntheticEvent<HTMLImageElement>) => void;

const hasImdbRating = (imdb: MovieCardImdb): imdb is NonNullable<MovieCardImdb> & { imdbRating: number } =>
  imdb?.imdbRating != null && imdb.imdbRating > 0;

/** Link target for a movie's IMDb badge (title page when the id is known). */
export const imdbBadgeHref = (imdb: MovieCardImdb): string =>
  imdb?.imdbUrl || (imdb?.imdbId ? 'https://www.imdb.com/title/' + imdb.imdbId + '/' : '#');

/** Link target for a movie title: the IMDb page, or an IMDb search as a fallback. */
export const imdbTitleLink = (imdb: MovieCardImdb, title: string, year?: number | string | null): string =>
  imdb?.imdbUrl ||
  (imdb?.imdbId
    ? 'https://www.imdb.com/title/' + imdb.imdbId + '/'
    : 'https://www.imdb.com/find/?q=' + encodeURIComponent([title, year].filter(Boolean).join(' ')));

const imdbBadgeTitle = (imdb: NonNullable<MovieCardImdb>) =>
  'IMDb rating' + (imdb.imdbVotes ? ' · ' + imdb.imdbVotes + ' votes' : '');

export interface MovieCompactCardProps {
  title: string;
  titleHref: string;
  year?: number | string | null;
  imdb?: MovieCardImdb;
  /** Shown only when there is no IMDb rating. */
  tmdbRating?: number | null;
  overview: string;
  posterSrc: string;
  posterAlt: string;
  onPosterClick: () => void;
  onPosterLoad: PosterEventHandler;
  onPosterError: PosterEventHandler;
  providers: MovieCardProvider[];
  providersAriaLabel: string;
  /** Text shown when there are no streaming providers to list. */
  providersFallbackText: string;
}

export const MovieCompactCard: React.FC<MovieCompactCardProps> = ({
  title,
  titleHref,
  year,
  imdb,
  tmdbRating,
  overview,
  posterSrc,
  posterAlt,
  onPosterClick,
  onPosterLoad,
  onPosterError,
  providers,
  providersAriaLabel,
  providersFallbackText,
}) => {
  const showImdb = hasImdbRating(imdb);
  const showTmdb = !showImdb && tmdbRating != null && tmdbRating > 0;
  return (
    <MovieCatalogueCard className="transition hover:border-slate-700 hover:bg-slate-900/95">
      <div className="relative aspect-[2/3] w-full overflow-hidden bg-slate-950">
        {posterSrc ? (
          <>
            <img
              src={posterSrc}
              alt={posterAlt}
              loading="lazy"
              className="h-full w-full object-cover cursor-pointer"
              onClick={onPosterClick}
              onLoad={event => {
                      const image = event.currentTarget;
                      onPosterLoad(event);
                      requestHighQualityPoster(image);
                    }}
                    onError={event => {
                      const image = event.currentTarget;
                      if (image.dataset.highQualityTried === 'true' && image.src.includes('/t/p/w780/')) {
                        image.src = image.src.replace('/t/p/w780/', '/t/p/w342/');
                        return;
                      }
                      onPosterError(event);
                    }}
            />
            <div data-poster-placeholder="true" onClick={onPosterClick} className="hidden cursor-pointer absolute inset-0 flex items-center justify-center text-slate-600">
              <Film className="h-8 w-8" />
            </div>
          </>
        ) : (
          <div className="absolute inset-0 flex items-center justify-center text-slate-600">
            <Film className="h-8 w-8" />
          </div>
        )}
        {showImdb ? (
          <a
            href={imdbBadgeHref(imdb)}
            target="_blank"
            rel="noreferrer"
            title={imdbBadgeTitle(imdb)}
            className="absolute left-2 top-2 rounded-md border border-amber-300/60 bg-slate-950/95 px-2 py-1 text-[10px] font-bold text-amber-300 shadow"
          >
            ⭐ {imdb.imdbRating.toFixed(1)}/10
          </a>
        ) : showTmdb ? (
          <span title="TMDB rating" className="absolute left-2 top-2 rounded-md border border-amber-300/40 bg-slate-950/95 px-2 py-1 text-[10px] font-bold text-amber-300 shadow">
            ⭐ TMDB {tmdbRating!.toFixed(1)}
          </span>
        ) : null}
      </div>

      <div className="flex flex-1 flex-col gap-2 p-2.5">
        <div>
          <h3 className="line-clamp-2 text-sm font-bold leading-snug text-slate-100">
            <a href={titleHref} target="_blank" rel="noreferrer" className="hover:text-cyan-300" title="Open on IMDb">
              {title}
            </a>
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] sm:text-xs">
            {year ? <span className="text-slate-400">{year}</span> : null}
            {showImdb ? (
              <a
                href={imdbBadgeHref(imdb)}
                target="_blank"
                rel="noreferrer"
                title={imdbBadgeTitle(imdb)}
                className="font-semibold text-amber-300 hover:text-amber-200"
              >
                IMDb {imdb.imdbRating.toFixed(1)}
              </a>
            ) : showTmdb ? (
              <span className="text-amber-300">TMDB {tmdbRating!.toFixed(1)}</span>
            ) : null}
          </div>
        </div>

        <p className="line-clamp-3 text-[10px] leading-relaxed text-slate-400">{overview}</p>

        {providers.length > 0 ? (
          <div className="flex flex-wrap gap-1" aria-label={providersAriaLabel}>
            {providers.slice(0, 5).map(provider => (
              <a
                key={provider.key}
                href={provider.webUrl || undefined}
                target={provider.webUrl ? '_blank' : undefined}
                rel={provider.webUrl ? 'noreferrer' : undefined}
                title={provider.name}
                className="rounded-full border border-cyan-900/70 bg-cyan-950/40 px-1.5 py-0.5 text-[10px] leading-4 text-cyan-200 hover:border-cyan-600"
              >
                {provider.name}
              </a>
            ))}
          </div>
        ) : (
          <div className="text-[10px] leading-relaxed text-slate-500">{providersFallbackText}</div>
        )}
      </div>
    </MovieCatalogueCard>
  );
};

export interface MoviePrepareCardVariant {
  key: string;
  label: string;
}

export interface MoviePrepareCardProps {
  posterSrc: string;
  posterAlt: string;
  onPosterClick: () => void;
  onPosterLoad: PosterEventHandler;
  onPosterError: PosterEventHandler;
  imdb?: MovieCardImdb;
  seeders?: number;
  indexer?: string;
  /** Short quality label such as "1080p" (badge on the poster). */
  quality?: string;
  title: string;
  titleHref: string;
  year?: number | string | null;
  onClose: () => void;
  variants: MoviePrepareCardVariant[];
  selectedVariantKey: string;
  onSelectVariant: (key: string) => void;
  variantsAriaLabel: string;
  /** Set once the torrent has been prepared in Seedr. */
  prepared: null | {
    isPlaying: boolean;
    isCopied: boolean;
    onPlay: () => void;
    onDownload: () => void;
    onCopy: () => void;
  };
  directStream: { label: string; disabled: boolean; onClick: () => void };
  prepare: { isPreparing: boolean; disabled: boolean; onClick: () => void };
  className?: string;
}

export const MoviePrepareCard: React.FC<MoviePrepareCardProps> = ({
  posterSrc,
  posterAlt,
  onPosterClick,
  onPosterLoad,
  onPosterError,
  imdb,
  seeders,
  indexer,
  quality,
  title,
  titleHref,
  year,
  onClose,
  variants,
  selectedVariantKey,
  onSelectVariant,
  variantsAriaLabel,
  prepared,
  directStream,
  prepare,
  className = '',
}) => {
  const showImdb = hasImdbRating(imdb);
  return (
    <div
      className={[
        'relative min-w-0 rounded-xl border border-slate-800 bg-slate-900 p-2 hover:bg-slate-800/80 transition sm:rounded-none sm:border-0 sm:bg-transparent sm:p-2 sm:px-4 sm:py-4 lg:flex lg:flex-col lg:self-start lg:h-fit lg:rounded-[14px] lg:border lg:border-slate-800 lg:bg-slate-900 lg:p-0 lg:hover:-translate-y-1 lg:hover:border-slate-700',
        className || 'z-0',
      ].join(' ')}
    >
      <div className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3 lg:flex-col lg:items-stretch lg:gap-0">
        <div className="min-w-0 flex-1 lg:w-full">
          <div className="flex flex-col sm:flex-row items-stretch sm:items-start gap-2 sm:gap-3 lg:flex-col lg:gap-0">
            <div className="relative w-full sm:w-24 shrink-0 aspect-[2/3] rounded-lg overflow-hidden border border-slate-800 bg-slate-950 shadow-md lg:w-full lg:rounded-none lg:border-0 lg:shadow-none">
              {posterSrc ? (
                <>
                  <img
                    src={posterSrc}
                    alt={posterAlt}
                    loading="lazy"
                    className="w-full h-full object-cover cursor-pointer"
                    onClick={onPosterClick}
                    onLoad={event => {
                      const image = event.currentTarget;
                      onPosterLoad(event);
                      requestHighQualityPoster(image);
                    }}
                    onError={event => {
                      const image = event.currentTarget;
                      if (image.dataset.highQualityTried === 'true' && image.src.includes('/t/p/w780/')) {
                        image.src = image.src.replace('/t/p/w780/', '/t/p/w342/');
                        return;
                      }
                      onPosterError(event);
                    }}
                  />
                  <div data-poster-placeholder="true" onClick={onPosterClick} className="hidden cursor-pointer absolute inset-0 flex items-center justify-center bg-slate-950 text-slate-500">
                    <Film className="w-7 h-7" />
                  </div>
                </>
              ) : (
                <div className="absolute inset-0 flex items-center justify-center bg-slate-950 text-slate-700">
                  <Film className="w-7 h-7" />
                </div>
              )}

              {showImdb && (
                <a
                  href={imdbBadgeHref(imdb)}
                  target="_blank"
                  rel="noreferrer"
                  title={imdbBadgeTitle(imdb)}
                  className="absolute left-2 top-2 rounded-md border border-amber-300/60 bg-slate-950/95 px-2 py-1 text-[10px] font-bold text-amber-300 shadow"
                >
                  ⭐ {imdb.imdbRating.toFixed(1)}/10
                </a>
              )}
              {Number(seeders) > 0 && (
                <div className={'absolute left-2 inline-flex items-center gap-1 rounded-md bg-black/80 px-2 py-1 text-[10px] font-bold text-emerald-400 ' + (showImdb ? 'top-10' : 'top-2')}>
                  <Users className="w-3 h-3" />
                  {seeders}
                </div>
              )}

              {indexer && (
                <div
                  className="absolute right-2 top-2 max-w-[70%] truncate rounded-md bg-black/75 px-2 py-1 text-[10px] font-medium text-slate-300"
                  title={indexer}
                >
                  {indexer}
                </div>
              )}

              {quality && (
                <div className="absolute left-2 bottom-2 rounded-md border border-cyan-400/30 bg-slate-950/85 px-2 py-1 text-[10px] font-bold text-cyan-300">
                  {quality}
                </div>
              )}
            </div>

            <div className="min-w-0 flex-1 lg:w-full lg:flex-none lg:p-3">
              <div className="flex items-start gap-2">
                <div className="hidden sm:flex lg:hidden p-1.5 rounded-lg bg-cyan-500/10 border border-cyan-500/20 shrink-0">
                  <Database className="w-3.5 h-3.5 text-cyan-400" />
                </div>
                <div className="relative min-w-0 flex-1 z-10">
                  <div className="flex items-start gap-1.5">
                    <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-1.5">
                      <a
                        href={titleHref}
                        target="_blank"
                        rel="noreferrer"
                        className="min-w-0 text-left text-[11px] leading-4 sm:text-sm font-semibold text-slate-100 hover:text-cyan-300 transition"
                        title="Open on IMDb"
                      >
                        {title}
                      </a>
                      {year ? <span className="shrink-0 text-[10px] sm:text-xs text-slate-500">{year}</span> : null}
                    </div>
                    <button
                      type="button"
                      onClick={onClose}
                      className="shrink-0 rounded-md p-1 text-slate-500 hover:bg-slate-800 hover:text-slate-100"
                      title="Back to search cards"
                      aria-label="Back to search cards"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>

                  {variants.length > 1 && (
                    <select
                      aria-label={variantsAriaLabel}
                      value={selectedVariantKey}
                      onChange={event => onSelectVariant(event.target.value)}
                      className="mt-2 w-full min-w-0 rounded-lg border border-slate-800 bg-slate-950 px-2 py-2 text-[10px] sm:text-xs font-medium text-slate-200 outline-none focus:border-cyan-500"
                      title="Choose quality and torrent source"
                    >
                      {variants.map(variant => (
                        <option key={variant.key} value={variant.key}>
                          {variant.label}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              </div>

              <div className="mt-2 flex items-center justify-between gap-2">
                {prepared ? (
                  <div className="min-w-0 max-w-full flex flex-wrap items-center justify-end gap-1.5">
                    <button
                      type="button"
                      disabled={prepared.isPlaying}
                      onClick={prepared.onPlay}
                      className="shrink-0 px-2 py-1.5 rounded-lg bg-emerald-400 text-slate-950 font-bold text-xs hover:bg-emerald-300 transition flex items-center gap-1 disabled:opacity-70 disabled:cursor-wait whitespace-nowrap"
                      title={prepared.isPlaying ? 'Opening stream…' : 'Play from Seedr'}
                    >
                      {prepared.isPlaying
                        ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        : <Play className="w-3.5 h-3.5" />}
                    </button>

                    <button
                      type="button"
                      onClick={prepared.onDownload}
                      className="shrink-0 p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white transition"
                      title="Download file"
                      aria-label="Download file"
                    >
                      <Download className="w-3.5 h-3.5" />
                    </button>

                    <button
                      type="button"
                      disabled={prepared.isCopied}
                      onClick={prepared.onCopy}
                      className="shrink-0 p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-cyan-400 transition disabled:opacity-60"
                      title="Copy download link"
                      aria-label="Copy download link"
                    >
                      {prepared.isCopied
                        ? <Check className="w-3.5 h-3.5 text-emerald-400" />
                        : <Copy className="w-3.5 h-3.5" />}
                    </button>
                  </div>
                ) : (
                  <div className="min-w-0 max-w-full">
                    <div className="flex w-full items-center justify-between gap-2">
                      <button
                        type="button"
                        disabled={directStream.disabled}
                        onClick={directStream.onClick}
                        className="min-w-0 flex-1 whitespace-nowrap rounded-lg border border-violet-400/40 bg-violet-500/15 px-1.5 py-1.5 text-[10px] font-bold text-violet-200 hover:bg-violet-500/25 disabled:opacity-60"
                      >
                        {directStream.label}
                      </button>
                      <div className="shrink-0">
                        <button
                          type="button"
                          disabled={prepare.disabled}
                          onClick={prepare.onClick}
                          className="shrink-0 whitespace-nowrap px-3 py-1.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 disabled:opacity-60 disabled:cursor-not-allowed text-slate-950 text-[11px] font-bold transition"
                        >
                          {prepare.isPreparing ? 'Preparing…' : 'Prepare'}
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
