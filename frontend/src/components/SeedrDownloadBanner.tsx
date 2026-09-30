import { Loader2 } from 'lucide-react';

type SeedrNotice = {
  name?: string | null;
  status?: string | null;
  progress?: number | string | null;
};

type SeedrDownloadBannerProps = {
  active: boolean;
  notice: SeedrNotice | null;
  cancelling: boolean;
  onCancel: () => void;
};

export function SeedrDownloadBanner({
  active,
  notice,
  cancelling,
  onCancel,
}: SeedrDownloadBannerProps) {
  if (!active || !notice) return null;

  const progress = Math.max(0, Math.min(100, Number(notice.progress) || 0));

  return (
    <div className="sticky top-2 z-40 mb-3 rounded-2xl border border-emerald-500/30 bg-slate-950/95 px-3.5 py-3.5 shadow-xl shadow-emerald-950/20 backdrop-blur-md sm:px-4">
      <div className="flex items-center gap-3">
        <Loader2 className="h-5 w-5 shrink-0 animate-spin text-emerald-400" />
        <div className="min-w-0 flex-1">
          <div className="text-xs font-bold text-slate-100 truncate">
            Loading {notice.name || 'torrent'} in Seedr
          </div>
          <div className="mt-0.5 flex items-center justify-between gap-3 text-[11px] text-slate-400">
            <span>
              {notice.status === 'waiting' ? 'Waiting for Seedr to start…' : 'Downloading…'}
            </span>
            <span className="font-semibold text-emerald-300">
              {progress.toFixed(1)}%
            </span>
          </div>
          <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-slate-800">
            <div
              className="h-full rounded-full bg-emerald-400 transition-all duration-500"
              style={{ width: progress + '%' }}
            />
          </div>
        </div>
        <button
          type="button"
          onClick={onCancel}
          disabled={cancelling}
          className="shrink-0 rounded-xl border border-rose-500/25 bg-rose-500/10 px-2.5 py-1.5 text-[10px] font-bold text-rose-300 hover:bg-rose-500/20 disabled:opacity-50"
        >
          {cancelling ? 'Cancelling…' : 'Cancel'}
        </button>
      </div>
    </div>
  );
}
