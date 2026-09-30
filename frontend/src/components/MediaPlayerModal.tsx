import React, { useState, useRef, useEffect } from 'react';
import {
  Play,
  Pause,
  Volume2,
  VolumeX,
  Maximize,
  Minimize,
  RotateCcw,
  RotateCw,
  ExternalLink,
  Download,
  Copy,
  Check,
  Captions,
  Languages,
  Music,
  Video,
  X,
  Minimize2,
  Maximize2,
  Loader2
} from 'lucide-react';
import Hls from 'hls.js';
import { api, API_BASE } from '../api/client.ts';
import { StorageFile } from '../types/index.ts';
import { formatBytes, formatDuration } from '../utils/formatters.ts';

type SubtitleCue = { start: number; end: number; text: string };

function parseWebVttTimestamp(value: string): number {
  const normalized = String(value || '').trim().replace(',', '.');
  const parts = normalized.split(':');
  if (parts.length === 3) {
    return Number(parts[0]) * 3600 + Number(parts[1]) * 60 + Number(parts[2]);
  }
  if (parts.length === 2) {
    return Number(parts[0]) * 60 + Number(parts[1]);
  }
  return Number(normalized) || 0;
}

function parseWebVttCues(source: string): SubtitleCue[] {
  const normalized = String(source || '')
    .replace(/^\uFEFF/, '')
    .replace(/\r\n?/g, '\n');

  return normalized
    .split(/\n\s*\n+/)
    .map(block => block.trim())
    .filter(block => block && !/^WEBVTT(?:\s|$)/i.test(block) && !/^NOTE(?:\s|$)/i.test(block) && !/^STYLE(?:\s|$)/i.test(block) && !/^REGION(?:\s|$)/i.test(block))
    .flatMap(block => {
      const lines = block.split('\n');
      const timingIndex = lines.findIndex(line => line.includes('-->'));
      if (timingIndex < 0) return [];

      const timing = lines[timingIndex].match(/^\s*(\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?|\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\s*-->\s*(\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?|\d{1,2}:\d{2}(?:[.,]\d{1,3})?)(?:\s+.*)?$/);
      if (!timing) return [];

      const start = parseWebVttTimestamp(timing[1]);
      const end = parseWebVttTimestamp(timing[2]);
      const text = lines
        .slice(timingIndex + 1)
        .join('\n')
        .replace(/<v\s+[^>]*>/gi, '')
        .replace(/<\/(?:v|c|i|b|u)>/gi, '')
        .replace(/<[^>]+>/g, '')
        .trim();

      if (!text || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) return [];
      return [{ start, end, text }];
    });
}

interface MediaPlayerModalProps {
  file: StorageFile | null;
  onClose: () => void;
  onPlaybackStarted?: () => void;
  isMinimized: boolean;
  onToggleMinimize: () => void;
}

export const MediaPlayerModal: React.FC<MediaPlayerModalProps> = ({
  file,
  onClose,
  onPlaybackStarted,
  isMinimized,
  onToggleMinimize
}) => {
  const [isPlaying, setIsPlaying] = useState(true);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [isMuted, setIsMuted] = useState(false);
  const [playbackSpeed, setPlaybackSpeed] = useState(1);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [fullscreenControlsVisible, setFullscreenControlsVisible] = useState(false);
  const [copied, setCopied] = useState(false);
  const [mediaError, setMediaError] = useState('');
  const [isSeeking, setIsSeeking] = useState(false);
  const [audioTracks, setAudioTracks] = useState<Array<{
    index: number; language: string; title: string; codec: string; channels: number; default: boolean;
  }>>([]);
  const [subtitleTracks, setSubtitleTracks] = useState<Array<{
    index: number; language: string; title: string; codec: string; url: string;
  }>>([]);
  const [hlsSubtitleTracks, setHlsSubtitleTracks] = useState<Array<{
    index: number; language: string; title: string; codec: string;
  }>>([]);
  const [selectedAudioIndex, setSelectedAudioIndex] = useState<number | undefined>(undefined);
  const [selectedSubtitleIndex, setSelectedSubtitleIndex] = useState<number | undefined>(undefined);
  const [selectedHlsSubtitleIndex, setSelectedHlsSubtitleIndex] = useState<number | undefined>(undefined);
  const [trackNotice, setTrackNotice] = useState('');
  const [subtitleSearchError, setSubtitleSearchError] = useState('');
  const [tracksLoading, setTracksLoading] = useState(false);
  const [subtitleBlobUrl, setSubtitleBlobUrl] = useState('');
  const [subtitleCues, setSubtitleCues] = useState<SubtitleCue[]>([]);

  const resumeTimeRef = useRef(0);
  const resumePlayingRef = useRef(false);
  const subtitleTrackRef = useRef<HTMLTrackElement>(null);
  const [usingDirectFallback, setUsingDirectFallback] = useState(false);
  const hlsActiveRef = useRef(false);
  const hlsRef = useRef<Hls | null>(null);

  const videoRef = useRef<HTMLVideoElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const alternateAudioRef = useRef<HTMLAudioElement>(null);
  const alternateAudioIndexRef = useRef<number | undefined>(undefined);
  const primaryAudioIndexRef = useRef<number | undefined>(undefined);
  const audioTrackCountRef = useRef(0);
  const isMutedRef = useRef(false);
  const companionAudioModeRef = useRef<'native' | 'alternate' | null>(null);
  const alternateAudioRequestRef = useRef(0);
  const containerRef = useRef<HTMLDivElement>(null);

  const isVideo = file?.type === 'video';
  const mediaRef = isVideo ? videoRef : audioRef;

  useEffect(() => {
    setCurrentTime(0);
    setDuration(0);
    setIsPlaying(true);
    setMediaError('');
    setIsSeeking(false);
    setIsMuted(false);
    isMutedRef.current = false;
    setUsingDirectFallback(false);
    hlsActiveRef.current = false;
    const initialSubtitles = file?.subtitleTracks || [];
    setSubtitleTracks(initialSubtitles);
    setHlsSubtitleTracks([]);
    setSelectedSubtitleIndex(undefined);
    setSelectedHlsSubtitleIndex(undefined);
    alternateAudioIndexRef.current = undefined;
    primaryAudioIndexRef.current = undefined;
    companionAudioModeRef.current = null;
    alternateAudioRequestRef.current += 1;
    const alternateAudio = alternateAudioRef.current;
    if (alternateAudio) {
      alternateAudio.pause();
      alternateAudio.removeAttribute('src');
      alternateAudio.load();
    }
    const initialAudioTracks = file?.audioTracks || [];
    audioTrackCountRef.current = initialAudioTracks.length;
    const initialAudio = initialAudioTracks.find((track: any) => track.default) || initialAudioTracks[0];
    primaryAudioIndexRef.current = initialAudio?.index;
    setAudioTracks(initialAudioTracks);
    setSelectedAudioIndex(initialAudio?.index);
  }, [file?.id]);


  useEffect(() => {
    const media = mediaRef.current;
    if (!media || !file) return;

    setMediaError('');
    setTrackNotice('Preparing browser stream…');

    // Always have a concrete browser source. Prefer the resolved backend
    // stream, then the direct Seedr presentation URL, then the download
    // endpoint as the final browser-playback fallback.
    const rawStreamUrl = String(file.streamUrl || '').trim();
    const directBaseUrl = rawStreamUrl.includes('/api/torrents/stream/')
      ? rawStreamUrl.replace('/api/torrents/stream/', '/api/torrents/direct-stream/')
      : rawStreamUrl;
    const streamUrl = rawStreamUrl || String(file.externalStreamUrl || '').trim() || String(file.downloadUrl || '').trim();

    const fallbackStreamUrl = String(file.externalStreamUrl || '').trim() &&
      String(file.externalStreamUrl || '').trim() !== streamUrl
      ? String(file.externalStreamUrl || '').trim()
      : String(file.downloadUrl || '').trim() !== streamUrl
        ? String(file.downloadUrl || '').trim()
        : '';

    const restoreTime = resumeTimeRef.current;
    const restorePlaying = resumePlayingRef.current || (!media.paused && duration > 0);

    const handleLoaded = () => {
      if (isVideo && streamUrl.includes('/api/seedr/media/video/')) {
        // Single-audio files must stay on Seedr's original native
        // video+audio presentation. Multi-audio only becomes muted after
        // the user explicitly selects an alternate track.
        media.volume = isMutedRef.current ? 0 : volume;
        media.muted =
          audioTrackCountRef.current > 1 &&
          alternateAudioIndexRef.current !== undefined
            ? true
            : isMutedRef.current;
      }
      if (Number.isFinite(restoreTime) && restoreTime > 0 && Number.isFinite(media.duration)) {
        const safeTime = Math.min(restoreTime, Math.max(0, media.duration - 0.25));
        try {
          media.currentTime = safeTime;
          setCurrentTime(safeTime);
        } catch {}
      }

      // A transient native MEDIA_ERR_SRC_NOT_SUPPORTED can be emitted while
      // Hls.js is attaching MediaSource. Clear any stale overlay once metadata
      // has successfully arrived.
      setMediaError('');

      // Stream buttons are an explicit user action, so always attempt to
      // start playback as soon as the browser has media metadata. The
      // <video>/<audio> elements also have autoPlay enabled below. If the
      // browser's autoplay policy blocks playback, the controls remain
      // available for a manual click.
      media.play()
        .then(() => setIsPlaying(true))
        .catch(() => setIsPlaying(false));
    };

    media.addEventListener('loadedmetadata', handleLoaded, { once: true });

    const handlePlaying = () => {
      // Stage 2 ends only when the browser is actually rendering playback.
      // This prevents the spinner from disappearing merely because metadata
      // or the first buffer arrived.
      setTrackNotice('');
      setMediaError('');
      setIsSeeking(false);
      setIsPlaying(true);
      onPlaybackStarted?.();
    };

    media.addEventListener('playing', handlePlaying);

    let hls: Hls | null = null;
    // Seedr's browser endpoint intentionally uses a clean same-origin
    // path instead of exposing the upstream .m3u8 filename. Treat that
    // endpoint as HLS explicitly.
    const isHlsStream =
      /\.m3u8(?:$|\?)/i.test(streamUrl) ||
      streamUrl.includes('/api/seedr/hls/') ||
      streamUrl.includes('/api/seedr/hls-master/') ||
      streamUrl.includes('/api/media/hls/');

    if (isHlsStream && isVideo && Hls.isSupported()) {
      hlsActiveRef.current = true;
      let triedFallback = false;

      const startHls = (sourceUrl: string) => {
        hls?.destroy();
        hls = new Hls({
          enableWorker: true,
          lowLatencyMode: false,
          backBufferLength: 90,
          // Render Free can take longer to produce the first on-demand HLS
          // segment. Give fragment requests more time and retry transient
          // segment failures before treating playback as fatal.
          fragLoadingTimeOut: 45000,
          fragLoadingMaxRetry: 3,
          fragLoadingRetryDelay: 1000,
          fragLoadingMaxRetryTimeout: 8000,
          // Seedr sessions are stored in an HttpOnly cookie on the Render API.
          // Hls.js does not send cross-origin cookies unless this is enabled.
          xhrSetup: (xhr) => {
            xhr.withCredentials = true;
          },
        });
        hlsRef.current = hls;
        setMediaError('');
        hls.loadSource(sourceUrl);
        hls.attachMedia(media as HTMLMediaElement);
        const syncHlsTracks = () => {
          const audio = (hls?.audioTracks || []).map((track: any, index: number) => ({
            index,
            language: String(track?.lang || track?.language || '').trim(),
            title: String(track?.name || track?.title || track?.lang || '').trim(),
            codec: String(track?.audioCodec || track?.codec || '').trim(),
            channels: Number(track?.channels || 0),
            default: Boolean(track?.default),
          }));
          const subtitles = (hls?.subtitleTracks || []).map((track: any, index: number) => ({
            index,
            language: String(track?.lang || track?.language || '').trim(),
            title: String(track?.name || track?.title || track?.lang || '').trim(),
            codec: String(track?.textCodec || track?.codec || '').trim(),
          }));
          setAudioTracks(audio);
          setHlsSubtitleTracks(subtitles);
          const defaultAudio = audio.find(track => track.default);
          if (selectedAudioIndex === undefined && defaultAudio) setSelectedAudioIndex(defaultAudio.index);
          if (selectedHlsSubtitleIndex !== undefined && !subtitles.some(track => track.index === selectedHlsSubtitleIndex)) {
            setSelectedHlsSubtitleIndex(undefined);
          }
        };
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          syncHlsTracks();
          setMediaError('');
        });
        hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, syncHlsTracks);
        hls.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, syncHlsTracks);
        hls.on(Hls.Events.AUDIO_TRACK_SWITCHED, () => {
          setTrackNotice('');
        });
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (!data?.fatal) return;

          console.warn('[MEDIA][HLS] fatal error', {
            details: data?.details,
            type: data?.type,
            fatal: data?.fatal,
            response: data?.response,
            networkDetails: data?.networkDetails,
            url: data?.url || sourceUrl,
          });

          if (
            !triedFallback &&
            fallbackStreamUrl &&
            sourceUrl !== fallbackStreamUrl &&
            !sourceUrl.includes('/api/seedr/hls-master/')
          ) {
            triedFallback = true;
            setTrackNotice('Trying browser-compatible stream…');
            startHls(fallbackStreamUrl);
            return;
          }

          setMediaError(data?.details || 'Unable to play the HLS stream.');
          setTrackNotice('');
          hls?.destroy();
          hls = null;
          hlsRef.current = null;
        });
      };

      startHls(streamUrl);
    } else if (isHlsStream && isVideo && media.canPlayType('application/vnd.apple.mpegurl')) {
      media.src = streamUrl;
      media.load();
    } else {
      media.src = streamUrl;
      media.load();
    }

    if (!isVideo) {
      media.play().then(() => setIsPlaying(true)).catch(() => setIsPlaying(false));
    }

    return () => {
      media.removeEventListener('loadedmetadata', handleLoaded);
      media.removeEventListener('playing', handlePlaying);
      if (hlsRef.current === hls) hlsRef.current = null;
      hls?.destroy();
      media.pause();
      media.removeAttribute('src');
      media.load();
      hlsActiveRef.current = false;
    };
  }, [file?.id, file?.streamUrl, file?.externalStreamUrl, isVideo]);



  useEffect(() => {
    if (!file || !isVideo) return;

    const fileId = file.streamId || file.id.replace(/^seedr-/, '');
    if (!fileId) return;

    const controller = new AbortController();
    let cancelled = false;
    setTracksLoading(true);

    api.getSeedrMediaInfo(fileId)
      .then(data => {
        if (cancelled || !data) return;

        const realAudio = Array.isArray(data.audioTracks) ? data.audioTracks : [];
        const embeddedSubtitles = Array.isArray(data.subtitleTracks) ? data.subtitleTracks : [];
        const sidecarSubtitles = Array.isArray(file.subtitleTracks) ? file.subtitleTracks : [];

        setAudioTracks(realAudio);
        audioTrackCountRef.current = realAudio.length;
        const defaultAudio = realAudio.find((track: any) => track.default) || realAudio[0];
        primaryAudioIndexRef.current = defaultAudio?.index;
        setSelectedAudioIndex(prev =>
          prev !== undefined && realAudio.some((track: any) => track.index === prev)
            ? prev
            : defaultAudio?.index
        );

        // Keep both embedded WebVTT tracks and Seedr sidecar tracks. The API
        // client converts embedded relative URLs to the Render API origin so
        // a Vercel-hosted frontend can actually load the subtitle file.
        const seen = new Set<string>();
        const mergedSubtitles = [...embeddedSubtitles, ...sidecarSubtitles].filter((track: any) => {
          const key = String(track.url || '') + '|' + String(track.title || '');
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        }).map((track: any, index: number) => ({ ...track, index }));

        setSubtitleTracks(mergedSubtitles);
        setSelectedSubtitleIndex(undefined);
        setSelectedHlsSubtitleIndex(undefined);
      })
      .finally(() => {
        if (!cancelled) setTracksLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [file?.id, file?.streamId, isVideo]);

  useEffect(() => {
    let cancelled = false;
    let objectUrl = '';

    if (selectedSubtitleIndex === undefined) {
      setSubtitleCues([]);
      setSubtitleBlobUrl(prev => {
        if (prev) URL.revokeObjectURL(prev);
        return '';
      });
      return () => {};
    }

    const selected = subtitleTracks.find(track => track.index === selectedSubtitleIndex);
    const sourceUrl = String(selected?.url || '').trim();
    if (!sourceUrl) {
      setSubtitleCues([]);
      return () => {};
    }

    setTrackNotice('Loading subtitles…');
    const subtitleUrl = sourceUrl.startsWith('/') ? API_BASE + sourceUrl : sourceUrl;
    void fetch(subtitleUrl, { credentials: 'include' })
      .then(async response => {
        if (!response.ok) throw new Error('Subtitle request failed: ' + response.status);
        const text = await response.text();
        if (!/^\\s*WEBVTT(?:\\s|$)/i.test(text)) {
          throw new Error('Subtitle response is not valid WebVTT');
        }
        setSubtitleCues(parseWebVttCues(text));
        return new Blob([text], { type: 'text/vtt' });
      })
      .then(blob => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSubtitleBlobUrl(prev => {
          if (prev) URL.revokeObjectURL(prev);
          return objectUrl;
        });
        setTrackNotice('');
      })
      .catch(error => {
        if (cancelled) return;
        console.warn('[MEDIA][SUBTITLES] failed to load', error);
        setSubtitleBlobUrl(prev => {
          if (prev) URL.revokeObjectURL(prev);
          return '';
        });
        setTrackNotice('Unable to load subtitles.');
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [selectedSubtitleIndex, subtitleTracks]);

  useEffect(() => {
    const trackElement = subtitleTrackRef.current;
    if (!trackElement || selectedSubtitleIndex === undefined || !subtitleBlobUrl) return;

    let cancelled = false;
    let retryTimer = 0;
    let attempts = 0;

    const syncNativeCues = () => {
      if (cancelled) return;

      try {
        trackElement.track.mode = 'showing';
      } catch {}

      const cues = trackElement.track.cues;
      if (cues && cues.length > 0) {
        const parsed: SubtitleCue[] = [];
        for (let i = 0; i < cues.length; i += 1) {
          const cue = cues[i] as TextTrackCue & { startTime: number; endTime: number; text?: string };
          const text = String(cue.text || '').replace(/<[^>]+>/g, '').trim();
          if (
            text &&
            Number.isFinite(cue.startTime) &&
            Number.isFinite(cue.endTime) &&
            cue.endTime > cue.startTime
          ) {
            parsed.push({
              start: cue.startTime,
              end: cue.endTime,
              text,
            });
          }
        }
        if (parsed.length > 0) {
          setSubtitleCues(parsed);
          setTrackNotice('');
          return;
        }
      }

      // Some Chromium builds populate TextTrack.cues one task after the
      // track load event. Give the native parser a few chances instead of
      // assuming an empty cue list means the subtitle file is empty.
      attempts += 1;
      if (attempts < 5) {
        retryTimer = window.setTimeout(syncNativeCues, 150);
      } else {
        setSubtitleCues([]);
        setTrackNotice('Subtitle file loaded, but no readable cues were found.');
      }
    };

    trackElement.addEventListener('load', syncNativeCues);
    syncNativeCues();

    return () => {
      cancelled = true;
      trackElement.removeEventListener('load', syncNativeCues);
      if (retryTimer) window.clearTimeout(retryTimer);
    };
  }, [selectedSubtitleIndex, subtitleBlobUrl]);

  useEffect(() => {
    const hls = hlsRef.current;
    if (!hls) return;
    if (selectedAudioIndex !== undefined && hls.audioTracks?.[selectedAudioIndex]) {
      hls.audioTrack = selectedAudioIndex;
    }
  }, [selectedAudioIndex, audioTracks]);

  useEffect(() => {
    const hls = hlsRef.current;
    if (!hls) return;
    if (selectedHlsSubtitleIndex === undefined) {
      hls.subtitleDisplay = false;
      hls.subtitleTrack = -1;
    } else if (hls.subtitleTracks?.[selectedHlsSubtitleIndex]) {
      hls.subtitleDisplay = true;
      hls.subtitleTrack = selectedHlsSubtitleIndex;
    }
  }, [selectedHlsSubtitleIndex, hlsSubtitleTracks]);



  // Keep React state synchronized with the browser's actual fullscreen state.
  useEffect(() => {
    const handleFullscreenChange = () => {
      const active = document.fullscreenElement === containerRef.current;
      setIsFullscreen(active);
      setFullscreenControlsVisible(false);
      if (active) {
        void lockLandscape();
      } else {
        unlockOrientation();
      }
    };

    document.addEventListener('fullscreenchange', handleFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange);
  }, [isVideo]);

  const activeSubtitleCue = subtitleCues.find(
    cue => currentTime >= cue.start && currentTime < cue.end
  );

  const handleMediaError = () => {
    // When Hls.js owns the video element, Chrome can briefly report
    // MEDIA_ERR_SRC_NOT_SUPPORTED while MediaSource is being attached.
    // Hls.js is the authoritative error source in that mode.
    if (hlsActiveRef.current) return;

    const media = mediaRef.current;
    if (
      media &&
      file?.externalStreamUrl &&
      file.externalStreamUrl !== file.streamUrl &&
      !usingDirectFallback
    ) {
      // The backend proxy is the preferred browser path, but Seedr's
      // presentation URL is known to be directly playable by Chrome for
      // some files. If the proxy response is rejected by the browser,
      // immediately retry the exact Seedr presentation URL rather than
      // showing a fatal error.
      setUsingDirectFallback(true);
      setMediaError('');
      setTrackNotice('Trying direct Seedr stream…');
      media.src = file.externalStreamUrl;
      media.load();
      return;
    }

    const code = media && 'error' in media ? media.error?.code : undefined;
    setMediaError(
      code ? `Browser could not play this stream (media error ${code}).` : 'Unable to play this video stream.'
    );
    setTrackNotice('');
    setIsSeeking(false);
    setIsPlaying(false);
    onPlaybackStarted?.();
  };

  // Toggle play/pause
  const togglePlay = () => {
    const media = mediaRef.current;
    const alternateAudio = alternateAudioRef.current;
    if (!media) return;

    if (isPlaying) {
      media.pause();
      alternateAudio?.pause();
      setIsPlaying(false);
      return;
    }

    const playRequests: Promise<any>[] = [media.play()];
    if (companionAudioModeRef.current !== null && alternateAudio?.src) {
      playRequests.push(alternateAudio.play());
    }
    Promise.allSettled(playRequests).then(() => {
      setIsPlaying(!media.paused);
    });
  };

  // Seek
  const handleSeek = (e: React.ChangeEvent<HTMLInputElement>) => {
    const time = parseFloat(e.target.value);
    const media = mediaRef.current;
    if (!media || !Number.isFinite(time)) return;

    const wasPlaying = !media.paused;
    setCurrentTime(time);
    setIsSeeking(true);
    setTrackNotice('Seeking…');

    media.currentTime = Math.max(0, Math.min(duration || file.duration || time, time));

    // When an alternate Seedr audio track is active, reload that audio from
    // the requested absolute video position. The video itself remains on its
    // original full-duration timeline.
    if (companionAudioModeRef.current === 'native') {
      const companion = alternateAudioRef.current;
      if (companion) {
        try {
          companion.currentTime = time;
        } catch {}
        if (wasPlaying) void companion.play().catch(() => {});
      }
      return;
    }

    if (companionAudioModeRef.current === 'alternate' && alternateAudioIndexRef.current !== undefined) {
      loadAlternateAudio(alternateAudioIndexRef.current, time, wasPlaying);
      return;
    }

    if (media.paused) {
      const clearPausedSeek = () => {
        setIsSeeking(false);
        setTrackNotice('');
        media.removeEventListener('seeked', clearPausedSeek);
      };
      media.addEventListener('seeked', clearPausedSeek, { once: true });
    }
  };

  // Skip
  const skip = (seconds: number) => {
    const media = mediaRef.current;
    if (!media) return;
    const nextTime = Math.max(0, Math.min(duration || file.duration || media.duration || 0, currentTime + seconds));
    handleSeek({ target: { value: String(nextTime) } } as React.ChangeEvent<HTMLInputElement>);
  };

  // Volume
  const handleVolume = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = parseFloat(e.target.value);
    setVolume(val);
    const muted = val === 0;
    setIsMuted(muted);
    isMutedRef.current = muted;
    if (mediaRef.current) {
      mediaRef.current.volume = val;
    }
    if (alternateAudioRef.current) {
      alternateAudioRef.current.volume = val;
      alternateAudioRef.current.muted = val === 0;
    }
  };

  const toggleMute = () => {
    const media = mediaRef.current;
    const alternateAudio = alternateAudioRef.current;
    if (!media) return;

    if (isMuted) {
      const nextVolume = volume || 0.8;
      media.volume = nextVolume;
      const externalAudioActive = companionAudioModeRef.current !== null;
      media.muted = externalAudioActive;
      if (alternateAudio) {
        alternateAudio.volume = nextVolume;
        alternateAudio.muted = false;
      }
      setIsMuted(false);
      isMutedRef.current = false;
    } else {
      media.volume = 0;
      media.muted = true;
      if (alternateAudio) {
        alternateAudio.volume = 0;
        alternateAudio.muted = true;
      }
      setIsMuted(true);
      isMutedRef.current = true;
    }
  };

  const loadAlternateAudio = (trackIndex: number, position: number, resumePlaying: boolean) => {
    const media = mediaRef.current;
    const audio = alternateAudioRef.current;
    const fileId = file?.streamId || file?.id?.replace(/^seedr-/, '');

    if (!media || !audio || !fileId) {
      setTrackNotice('Selected audio track is not available in this stream.');
      return;
    }

    const requestId = ++alternateAudioRequestRef.current;
    const safePosition = Math.max(0, Number.isFinite(position) ? position : 0);

    media.pause();
    media.muted = true;
    audio.pause();
    audio.volume = media.volume;
    audio.muted = isMuted;
    audio.playbackRate = playbackSpeed;

    setTrackNotice('Switching audio…');
    setMediaError('');
    setIsSeeking(true);
    companionAudioModeRef.current = 'alternate';
    alternateAudioIndexRef.current = trackIndex;

    let started = false;
    let readyTimeout = 0;

    const finish = () => {
      if (requestId !== alternateAudioRequestRef.current || started) return;
      started = true;
      if (readyTimeout) window.clearTimeout(readyTimeout);
      audio.removeEventListener('loadedmetadata', startTogether);
      audio.removeEventListener('loadeddata', startTogether);
      audio.removeEventListener('canplay', startTogether);
      audio.removeEventListener('error', handleAudioError);

      const protocol = String(audio.dataset.playbackProtocol || '');
      const targetTime = protocol === 'ffmpeg-audio-fallback' ? 0 : safePosition;

      try {
        if (Number.isFinite(audio.duration) && audio.duration > 0) {
          audio.currentTime = Math.min(targetTime, Math.max(0, audio.duration - 0.1));
        } else {
          audio.currentTime = targetTime;
        }
      } catch {}

      if (!resumePlaying) {
        setIsSeeking(false);
        setIsPlaying(false);
        setTrackNotice('');
        return;
      }

      const startVideo = media.play();
      const startAudio = audio.play();
      Promise.allSettled([startVideo, startAudio]).then((results) => {
        if (requestId !== alternateAudioRequestRef.current) return;
        const videoStarted = results[0]?.status === 'fulfilled';
        const audioStarted = results[1]?.status === 'fulfilled';
        if (!videoStarted || !audioStarted) {
          media.pause();
          audio.pause();
          setIsSeeking(false);
          setIsPlaying(false);
          setTrackNotice('Playback could not resume with the selected audio track.');
          return;
        }
        setIsSeeking(false);
        setIsPlaying(true);
        setTrackNotice('');
      });
    };

    const startTogether = () => {
      // loadedmetadata is enough for the audio-only MP4 fallback; waiting
      // exclusively for canplay could leave the switch spinner stuck on a
      // long-running Render response.
      finish();
    };

    const handleAudioError = () => {
      if (requestId !== alternateAudioRequestRef.current || started) return;
      if (readyTimeout) window.clearTimeout(readyTimeout);
      audio.removeEventListener('loadedmetadata', startTogether);
      audio.removeEventListener('loadeddata', startTogether);
      audio.removeEventListener('canplay', startTogether);
      alternateAudioIndexRef.current = undefined;
      companionAudioModeRef.current = null;
      media.muted = isMutedRef.current;
      setIsSeeking(false);
      setIsPlaying(false);
      setTrackNotice('Unable to load the selected audio track.');
    };

    audio.addEventListener('loadedmetadata', startTogether);
    audio.addEventListener('loadeddata', startTogether);
    audio.addEventListener('canplay', startTogether);
    audio.addEventListener('error', handleAudioError, { once: true });

    // Do not leave the player permanently blocked if a streaming response
    // provides no readiness event. Give the browser a chance to start once
    // headers/metadata have arrived, then surface a real playback error.
    readyTimeout = window.setTimeout(() => {
      if (!started && requestId === alternateAudioRequestRef.current) {
        setTrackNotice('Audio stream is taking too long to start.');
        setIsSeeking(false);
      }
    }, 15000);

    void api.getSeedrAudioPresentationUrl(fileId, trackIndex, safePosition)
      .then(({ url, protocol, start }) => {
        if (requestId !== alternateAudioRequestRef.current) return;
        if (!url) throw new Error('Seedr returned an empty audio playback URL');

        audio.dataset.playbackProtocol = String(protocol || '');
        audio.dataset.playbackStart = String(Number(start) || 0);
        audio.src = url;
        audio.load();
      })
      .catch((error) => {
        if (requestId !== alternateAudioRequestRef.current) return;
        console.warn('[MEDIA][AUDIO] alternate track load failed', error);
        handleAudioError();
      });
  };

  const clearAlternateAudio = (restoreVideoAudio = true) => {
    const media = mediaRef.current;
    const audio = alternateAudioRef.current;
    alternateAudioRequestRef.current += 1;

    if (audio) {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    }
    alternateAudioIndexRef.current = undefined;
    companionAudioModeRef.current = null;

    if (restoreVideoAudio && media) {
      media.volume = isMutedRef.current ? 0 : volume;
      media.muted = isMutedRef.current;
    }
  };

  const handleAudioTrackChange = (value: string) => {
    const next = Number(value);
    if (!Number.isInteger(next)) return;

    const media = mediaRef.current;
    const hls = hlsRef.current;

    // HLS multi-audio is the primary path for files with multiple embedded
    // audio tracks. Let hls.js switch the rendition inside the same media
    // pipeline so video/audio remain synchronized.
    if (hls && hls.audioTracks?.[next]) {
      const wasPlaying = Boolean(media && !media.paused);
      setSelectedAudioIndex(next);
      hls.audioTrack = next;
      setTrackNotice('Switching audio…');

      const handleSwitched = (_event: any, data: any) => {
        if (Number(data?.id) !== next) return;
        hls.off(Hls.Events.AUDIO_TRACK_SWITCHED, handleSwitched);
        setTrackNotice('');
        setIsPlaying(Boolean(media && !media.paused));
      };

      hls.on(Hls.Events.AUDIO_TRACK_SWITCHED, handleSwitched);

      // If hls.js has already completed the switch synchronously, don't leave
      // the loading notice visible.
      if (!wasPlaying && media?.paused) {
        setTrackNotice('');
      }
      return;
    }

    // Direct Seedr presentation path: keep the video on its full-duration
    // Range timeline and route ALL embedded audio tracks through the separate
    // audio element. Seedr's native video presentation is not guaranteed to
    // expose the embedded audio reliably to the browser.
    if (media && file?.streamUrl?.includes('/api/seedr/media/video/')) {
      const position = Number.isFinite(media.currentTime) ? media.currentTime : currentTime;
      const wasPlaying = !media.paused;
      setSelectedAudioIndex(next);

      // Single-audio direct streams use Seedr's native video+audio.
      if (audioTracks.length <= 1) {
        clearAlternateAudio(true);
        media.volume = isMutedRef.current ? 0 : volume;
        media.muted = isMutedRef.current;
        media.currentTime = position;
        if (wasPlaying) {
          media.play()
            .then(() => {
              setIsPlaying(true);
              setTrackNotice('');
            })
            .catch(() => setIsPlaying(false));
        } else {
          setIsPlaying(false);
          setTrackNotice('');
        }
        return;
      }

      loadAlternateAudio(next, position, wasPlaying);
      return;
    }

    setTrackNotice('Selected audio track is not available in this stream.');
  };
  // Hybrid audio strategy:
  // - Single-audio direct streams keep Seedr's native video+audio together.
  // - Multi-audio direct streams use a second synchronized audio element only
  //   when the user selects an alternate track.
  useEffect(() => {
    if (!isVideo || !file?.streamUrl?.includes('/api/seedr/media/video/')) return;
    if (!mediaRef.current || audioTracks.length === 0 || selectedAudioIndex === undefined) return;
    if (hlsActiveRef.current) return;

    const media = mediaRef.current;
    const isMultiAudio = audioTracks.length > 1;
    const primary = primaryAudioIndexRef.current;

    if (!isMultiAudio) {
      // Single-audio files stay entirely on Seedr's native video+audio
      // presentation. Do not create a second audio request here: Seedr's
      // legacy MP3 endpoint can reject session credentials, while the native
      // video presentation is the lowest-load and properly synchronized path.
      if (alternateAudioIndexRef.current !== undefined || companionAudioModeRef.current !== null) {
        clearAlternateAudio(true);
      }
      media.volume = isMutedRef.current ? 0 : volume;
      media.muted = isMutedRef.current;
      return;
    }

    // Multi-audio: keep the primary track on native video audio until an
    // alternate track is selected.
    if (primary !== undefined && selectedAudioIndex === primary) {
      if (alternateAudioIndexRef.current !== undefined) {
        clearAlternateAudio(true);
      }
      media.volume = isMutedRef.current ? 0 : volume;
      media.muted = isMutedRef.current;
      return;
    }

    if (
      alternateAudioIndexRef.current === selectedAudioIndex &&
      alternateAudioRef.current?.src
    ) {
      media.muted = true;
      return;
    }

    const position = Number.isFinite(media.currentTime) ? media.currentTime : currentTime;
    const shouldPlay = !media.paused || isPlaying;
    loadAlternateAudio(selectedAudioIndex, position, shouldPlay);
  }, [audioTracks, selectedAudioIndex, file?.streamUrl, isVideo]);

  const handleSubtitleTrackChange = (value: string) => {
    if (value === 'off') {
      setSelectedSubtitleIndex(undefined);
      setSelectedHlsSubtitleIndex(undefined);
      return;
    }
    if (value.startsWith('hls:')) {
      const next = Number(value.slice(4));
      if (Number.isInteger(next)) {
        setSelectedSubtitleIndex(undefined);
        setSelectedHlsSubtitleIndex(next);
      }
      return;
    }
    if (value.startsWith('external:')) {
      const next = Number(value.slice(9));
      if (Number.isInteger(next)) {
        setSelectedHlsSubtitleIndex(undefined);
        setSelectedSubtitleIndex(next);
      }
    }
  };

  // Speed
  const handleSpeedChange = (speed: number) => {
    setPlaybackSpeed(speed);
    if (mediaRef.current) {
      mediaRef.current.playbackRate = speed;
    }
    if (alternateAudioRef.current) {
      alternateAudioRef.current.playbackRate = speed;
    }
  };

  const languageNames: Record<string, string> = {
    en: 'English', eng: 'English',
    hi: 'Hindi', hin: 'Hindi',
    fr: 'French', fra: 'French',
    de: 'German', deu: 'German',
    es: 'Spanish', spa: 'Spanish',
    it: 'Italian', ita: 'Italian',
    pt: 'Portuguese', por: 'Portuguese',
    ru: 'Russian', rus: 'Russian',
    ja: 'Japanese', jpn: 'Japanese',
    ko: 'Korean', kor: 'Korean',
    zh: 'Chinese', zho: 'Chinese',
    ar: 'Arabic', ara: 'Arabic',
    bn: 'Bengali', ben: 'Bengali',
  };

  const formatTrackLabel = (
    track: { language?: string; title?: string },
    index: number,
    fallbackPrefix: string
  ) => {
    const languageCode = String(track.language || '').trim().toLowerCase();
    const language = languageNames[languageCode] || languageCode.toUpperCase();
    const title = String(track.title || '').trim();
    if (language && title && title.toLowerCase() !== language.toLowerCase()) {
      return language + ' (' + title + ')';
    }
    return language || title || fallbackPrefix + ' ' + (index + 1);
  };

  // Fullscreen
  const lockLandscape = async () => {
    if (!isVideo) return;
    try {
      if (typeof screen !== 'undefined' && screen.orientation?.lock) {
        await screen.orientation.lock('landscape');
      }
    } catch {
      // Some Android browsers expose fullscreen but do not allow orientation
      // locking. The fullscreen layout below still uses the real viewport.
    }
  };

  const unlockOrientation = () => {
    try {
      if (typeof screen !== 'undefined' && screen.orientation?.unlock) {
        screen.orientation.unlock();
      }
    } catch {
      // Orientation unlock is not supported by every browser.
    }
  };

  const toggleFullscreen = async () => {
    if (!containerRef.current) return;

    if (!document.fullscreenElement) {
      try {
        await containerRef.current.requestFullscreen?.();
        setIsFullscreen(true);
        await lockLandscape();
      } catch {
        setIsFullscreen(Boolean(document.fullscreenElement));
      }
    } else {
      try {
        await document.exitFullscreen?.();
      } finally {
        setIsFullscreen(false);
        unlockOrientation();
      }
    }
  };

  // Copy Direct Stream URL
  const copyStreamUrl = () => {
    // For Seedr files, prefer the exact external-player HLS URL generated by
    // the backend. Otherwise copy the app's same-origin stream URL.
    const fullUrl = file.externalStreamUrl || (
      file.streamUrl.startsWith('http://') || file.streamUrl.startsWith('https://')
        ? file.streamUrl
        : window.location.origin + file.streamUrl
    );
    navigator.clipboard.writeText(fullUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // Picture in Picture
  const togglePip = async () => {
    if (videoRef.current && document.pictureInPictureEnabled) {
      if (document.pictureInPictureElement) {
        await document.exitPictureInPicture();
      } else {
        await videoRef.current.requestPictureInPicture();
      }
    }
  };

  // Timeline seeking/buffering
  const onSeeking = () => {
    setIsSeeking(true);
    setTrackNotice('Seeking…');
  };

  const onSeeked = () => {
    const media = mediaRef.current;
    // If playback was paused, "playing" will never arrive to clear the
    // loader. For active playback, keep it visible until "playing" resumes.
    if (media?.paused) {
      setIsSeeking(false);
      setTrackNotice('');
    }
  };

  // Time update
  const onTimeUpdate = () => {
    const media = mediaRef.current;
    if (!media) return;

    setCurrentTime(media.currentTime);

    const companion = alternateAudioRef.current;
    if (
      isVideo &&
      companionAudioModeRef.current !== null &&
      companion &&
      !companion.paused &&
      Number.isFinite(companion.currentTime)
    ) {
      const drift = companion.currentTime - media.currentTime;
      if (Math.abs(drift) > 0.35) {
        try {
          companion.currentTime = media.currentTime;
        } catch {}
      }
    }
  };

  const onLoadedMetadata = () => {
    if (mediaRef.current) {
      setDuration(mediaRef.current.duration || file.duration || 600);

      if (resumePlayingRef.current) {
        mediaRef.current.play().then(() => setIsPlaying(true)).catch(() => setIsPlaying(false));
      } else {
        setIsPlaying(false);
      }
    }
  };

  if (!file) return null;

  // Minimized floating player (for multitasking while downloading or browsing folders)
  if (isMinimized) {
    return (
      <div className="fixed bottom-16 md:bottom-6 right-4 z-50 w-80 md:w-96 bg-slate-900/95 backdrop-blur-xl border border-slate-700 shadow-2xl rounded-2xl p-3.5 flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 overflow-hidden">
            <div className="p-2 rounded-lg bg-cyan-500/10 text-cyan-400">
              {isVideo ? <Video className="w-4 h-4" /> : <Music className="w-4 h-4" />}
            </div>
            <div className="truncate">
              <p className="text-xs font-semibold text-slate-200 truncate">{file.name}</p>
              <p className="text-[10px] text-slate-400">{formatDuration(currentTime)} / {formatDuration(duration || file.duration || 0)}</p>
            </div>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={onToggleMinimize}
              className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-slate-200"
              title="Expand"
            >
              <Maximize2 className="w-4 h-4" />
            </button>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-slate-200"
              title="Close Player"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Hidden or small video preview */}
        {isVideo ? (
          <video
            ref={videoRef}
            crossOrigin={file.streamUrl?.startsWith(API_BASE) ? 'anonymous' : undefined}
            src={file.streamUrl || file.externalStreamUrl || file.downloadUrl}
            className="w-full h-32 object-contain bg-black rounded-lg"
            onTimeUpdate={onTimeUpdate}
            onSeeking={onSeeking}
            onSeeked={onSeeked}
            onPlaying={() => {
              setIsSeeking(false);
              setTrackNotice('');
              setIsPlaying(true);
            }}
            onLoadedMetadata={onLoadedMetadata}
            onEnded={() => setIsPlaying(false)}
          />
        ) : (
          <audio
            ref={audioRef}
            autoPlay
            src={file.streamUrl || file.externalStreamUrl || file.downloadUrl}
            onTimeUpdate={onTimeUpdate}
            onLoadedMetadata={onLoadedMetadata}
            onEnded={() => setIsPlaying(false)}
          />
        )}

        <audio ref={alternateAudioRef} preload="auto" className="hidden" aria-hidden="true" />

        {/* Mini Controls */}
        <div className="flex items-center justify-between pt-1">
          <button
            onClick={() => skip(-10)}
            className="p-1.5 rounded text-slate-400 hover:text-slate-200"
          >
            <RotateCcw className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={togglePlay}
            className="p-2 rounded-full bg-cyan-500 text-slate-950 font-bold hover:bg-cyan-400 transition"
          >
            {isPlaying ? <Pause className="w-4 h-4 fill-current" /> : <Play className="w-4 h-4 fill-current" />}
          </button>
          <button
            onClick={() => skip(10)}
            className="p-1.5 rounded text-slate-400 hover:text-slate-200"
          >
            <RotateCw className="w-3.5 h-3.5" />
          </button>
          <div className="flex items-center gap-1.5 ml-2">
            <button onClick={toggleMute} className="text-slate-400 hover:text-slate-200">
              {isMuted ? <VolumeX className="w-3.5 h-3.5" /> : <Volume2 className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>

        {/* Scrubber */}
        <input
          type="range"
          min={0}
          max={duration || file.duration || 100}
          value={currentTime}
          onChange={handleSeek}
          className="w-full h-1 bg-slate-700 rounded-lg appearance-none cursor-pointer accent-cyan-400"
        />
      </div>
    );
  }

  // Full Player Modal
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-2 sm:p-4 md:p-6 bg-black/80 backdrop-blur-md">
      <div
        ref={containerRef}
        className={`relative bg-slate-900 overflow-hidden flex flex-col ${
          isFullscreen
            ? 'w-screen h-screen max-w-none max-h-none rounded-none border-0'
            : 'w-full max-w-4xl border border-slate-700/80 rounded-2xl shadow-2xl max-h-[95vh]'
        }`}
      >
        {/* Top Header */}
        <div className={`${isFullscreen ? 'hidden' : 'flex'} items-center justify-between px-4 py-3 border-b border-slate-800 bg-slate-900/90 z-10`}>
          <div className="flex items-center gap-3 overflow-hidden">
            <div className="p-2 rounded-xl bg-cyan-500/10 text-cyan-400">
              {isVideo ? <Video className="w-5 h-5" /> : <Music className="w-5 h-5" />}
            </div>
            <div className="truncate">
              <h3 className="text-sm md:text-base font-semibold text-slate-100 truncate">{file.name}</h3>
              <p className="text-xs text-slate-400 flex items-center gap-2">
                <span>{formatBytes(file.size)}</span>
                <span>•</span>
                <span className="text-emerald-400 font-medium">
                  Direct Browser Streaming
                </span>
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={copyStreamUrl}
              className="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-medium text-slate-300 flex items-center gap-1.5 transition"
              title="Copy Direct Streaming Link"
            >
              {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
              <span className="hidden sm:inline">{copied ? 'Copied' : 'Stream URL'}</span>
            </button>

            <a
              href={file.downloadUrl}
              download={file.name}
              className="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-medium text-slate-300 flex items-center gap-1.5 transition"
              title="Direct Download File"
            >
              <Download className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Download</span>
            </a>

            <button
              onClick={onToggleMinimize}
              className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-slate-200 transition"
              title="Minimize to Floating Player"
            >
              <Minimize2 className="w-4 h-4" />
            </button>

            <button
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-slate-200 transition"
              title="Close Player"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Media Viewport */}
        <div
          className={`relative flex-1 min-h-0 bg-black flex items-center justify-center overflow-hidden ${
            isFullscreen ? 'h-full min-h-0' : 'min-h-[260px] md:min-h-[420px]'
          }`}
          onClick={() => {
            if (isFullscreen) {
              setFullscreenControlsVisible(current => !current);
            }
          }}
        >
          {mediaError && (
            <div className="absolute inset-0 z-10 flex items-center justify-center p-6 text-center">
              <div className="max-w-md rounded-xl bg-slate-900/95 border border-rose-500/30 p-5">
                <p className="text-sm font-semibold text-rose-300">{mediaError}</p>
                <p className="text-xs text-slate-400 mt-2">
                  The Seedr stream could not be played. We tried the direct Seedr presentation URL and the server proxy.
                </p>
              </div>
            </div>
          )}

          {/* Stage 2: the stream URL is ready and the player is now opening it.
              Keep this lightweight overlay visible until browser media
              metadata arrives so the player never looks frozen/empty. */}
          {!mediaError && trackNotice && (
            <div className="absolute inset-0 z-10 flex items-center justify-center p-6 text-center pointer-events-none">
              <div className="rounded-xl bg-slate-900/90 border border-cyan-500/20 px-5 py-4 shadow-xl">
                <div className="flex items-center justify-center gap-2 text-cyan-300">
                  <span className="inline-flex w-5 h-5 rounded-full border-2 border-cyan-300/30 border-t-cyan-300 animate-spin" />
                  <span className="text-sm font-semibold">{trackNotice}</span>
                </div>
                <p className="text-[11px] text-slate-400 mt-1.5">
                  Connecting to the browser stream…
                </p>
              </div>
            </div>
          )}

          {isVideo ? (
            <>
            <video
              ref={videoRef}
              crossOrigin={file.streamUrl?.startsWith(API_BASE) ? 'anonymous' : undefined}
              autoPlay
              className={`w-full h-full object-contain cursor-pointer ${
                isFullscreen ? 'max-h-none' : 'max-h-[60vh]'
              }`}
              onClick={(event) => {
                event.stopPropagation();
                if (isFullscreen) {
                  setFullscreenControlsVisible(current => !current);
                }
              }}
              onTimeUpdate={onTimeUpdate}
              onSeeking={onSeeking}
              onSeeked={onSeeked}
              onPlaying={() => {
                setIsSeeking(false);
                setTrackNotice('');
                setIsPlaying(true);
              }}
              onLoadedMetadata={onLoadedMetadata}
              onEnded={() => setIsPlaying(false)}
              onError={handleMediaError}>
              {selectedSubtitleIndex !== undefined && subtitleBlobUrl && (
                <track
                  ref={subtitleTrackRef}
                  kind="subtitles"
                  src={subtitleBlobUrl}
                  srcLang={subtitleTracks.find(track => track.index === selectedSubtitleIndex)?.language || 'en'}
                  label={subtitleTracks.find(track => track.index === selectedSubtitleIndex)?.title || 'Subtitles'}
                  default
                />
              )}
            </video>
            {activeSubtitleCue && selectedSubtitleIndex !== undefined && (
              <div className="absolute left-1/2 bottom-4 z-20 max-w-[90%] -translate-x-1/2 rounded-md bg-black/75 px-3 py-1.5 text-center text-sm font-medium leading-6 text-white shadow-lg backdrop-blur-sm whitespace-pre-line pointer-events-none md:text-base">
                {activeSubtitleCue.text}
              </div>
            )}
            </>
          ) : (
            <div className="flex flex-col items-center justify-center p-8 text-center gap-4">
              <div className="w-24 h-24 rounded-full bg-gradient-to-tr from-cyan-500 to-indigo-600 flex items-center justify-center shadow-lg shadow-cyan-500/20 animate-pulse-subtle">
                <Music className="w-12 h-12 text-white" />
              </div>
              <div>
                <h4 className="text-lg font-bold text-slate-100">{file.name}</h4>
                <p className="text-sm text-slate-400 mt-1">Lossless Cloud Audio Playback</p>
              </div>

              {/* Dynamic waveform simulation */}
              <div className="flex items-center gap-1 h-12 mt-2">
                {[40, 65, 30, 85, 95, 45, 75, 55, 90, 60, 35, 70, 80, 50, 65, 85, 40, 70].map((h, i) => (
                  <div
                    key={i}
                    className="w-1.5 bg-gradient-to-t from-cyan-500 to-indigo-400 rounded-full transition-all duration-300"
                    style={{
                      height: isPlaying ? `${Math.max(12, (h * (0.4 + (i % 3) * 0.3)))}px` : '8px',
                      opacity: isPlaying ? 1 : 0.4
                    }}
                  />
                ))}
              </div>

              <audio
                ref={audioRef}
                src={file.streamUrl}
                onTimeUpdate={onTimeUpdate}
                onLoadedMetadata={onLoadedMetadata}
                onEnded={() => setIsPlaying(false)}
              />
            </div>
          )}
          <audio ref={alternateAudioRef} preload="auto" className="hidden" aria-hidden="true" />
        </div>

        {/* Player Controls Bar */}
        <div
          onClick={(event) => event.stopPropagation()}
          className={`p-4 bg-slate-900/95 border-t border-slate-800 flex flex-col gap-3 ${
            isFullscreen
              ? 'absolute bottom-0 left-0 right-0 z-20 backdrop-blur-md transition-opacity duration-200 ' +
                (fullscreenControlsVisible ? 'opacity-100' : 'opacity-0 pointer-events-none')
              : ''
          }`}
        >
          {/* Scrubber and Time */}
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-400 w-12 text-right">
              {formatDuration(currentTime)}
            </span>
            <div className="relative flex-1 group">
              <input
                type="range"
                min={0}
                max={duration || file.duration || 100}
                value={currentTime}
                onChange={handleSeek}
                className="w-full h-2 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-cyan-400 hover:h-2.5 transition-all"
              />
            </div>
            <span className="text-xs font-mono text-slate-400 w-12">
              {formatDuration(duration || file.duration || 0)}
            </span>
          </div>

          {/* Main Controls */}
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-2 min-w-0">
              <div className="flex items-center gap-1.5 shrink-0">
                <button onClick={() => skip(-10)} className="p-2 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 transition" title="Rewind 10 seconds"><RotateCcw className="w-4 h-4" /></button>
                <button onClick={togglePlay} className="p-3 rounded-xl bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold transition shadow-lg shadow-cyan-500/20" title={isPlaying ? 'Pause' : 'Play'}>
                  {isPlaying ? <Pause className="w-5 h-5 fill-current" /> : <Play className="w-5 h-5 fill-current" />}
                </button>
                <button onClick={() => skip(10)} className="p-2 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 transition" title="Forward 10 seconds"><RotateCw className="w-4 h-4" /></button>
                <div className="flex items-center gap-1.5 ml-1.5 pl-1.5 border-l border-slate-800">
                  <button onClick={toggleMute} className="p-2 rounded-lg text-slate-400 hover:text-slate-200" title={isMuted ? 'Unmute' : 'Mute'}>
                    {isMuted ? <VolumeX className="w-4 h-4" /> : <Volume2 className="w-4 h-4" />}
                  </button>
                  <input type="range" min={0} max={1} step={0.05} value={isMuted ? 0 : volume} onChange={handleVolume}
                    className="w-14 sm:w-20 h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-cyan-400" title="Volume" />
                </div>
              </div>
              <div className="flex items-center gap-1.5 shrink-0">
                {tracksLoading && <div className="flex items-center gap-1.5 bg-slate-800/80 rounded-lg px-2 py-1.5 text-[11px] text-slate-400"><Loader2 className="w-3.5 h-3.5 animate-spin text-cyan-400" /><span className="hidden sm:inline">Tracks</span></div>}
                {isVideo && <button onClick={togglePip} className="p-2 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 transition" title="Picture in Picture"><ExternalLink className="w-4 h-4" /></button>}
                <button onClick={toggleFullscreen} className="p-2 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 transition" title="Fullscreen">
                  {isFullscreen ? <Minimize className="w-4 h-4" /> : <Maximize className="w-4 h-4" />}
                </button>
              </div>
            </div>

            <div className="flex items-center gap-1.5 flex-wrap min-w-0">
              {audioTracks.length > 1 && (
                <label className="flex items-center gap-1.5 rounded-lg border border-slate-700/80 bg-slate-800/90 px-2 py-1.5 min-w-0 flex-1 sm:flex-none">
                  <Languages className="h-4 w-4 shrink-0 text-cyan-400" />
                  <span className="hidden sm:inline text-[10px] font-semibold uppercase tracking-wide text-slate-400">Audio</span>
                  <select value={selectedAudioIndex !== undefined ? selectedAudioIndex : (audioTracks[0]?.index ?? '')}
                    onChange={(e) => handleAudioTrackChange(e.target.value)}
                    className="min-w-0 w-full sm:w-[180px] bg-slate-800 text-xs font-semibold text-slate-100 outline-none"
                    style={{ colorScheme: 'dark' }} title="Audio track">
                    {audioTracks.map((track, index) => (
                      <option key={track.index} value={track.index} className="bg-slate-900 text-slate-100">{formatTrackLabel(track, index, 'Audio')}{track.default ? ' · Default' : ''}</option>
                    ))}
                  </select>
                </label>
              )}

              {isVideo && (
                <label className="flex items-center gap-1.5 rounded-lg border border-slate-700/80 bg-slate-800/90 px-2 py-1.5 min-w-0 flex-1 sm:flex-none">
                  <Captions className="h-4 w-4 shrink-0 text-cyan-400" />
                  <span className="hidden sm:inline text-[10px] font-semibold uppercase tracking-wide text-slate-400">Subs</span>
                  <select disabled={tracksLoading || (hlsSubtitleTracks.length === 0 && subtitleTracks.length === 0)}
                    value={selectedHlsSubtitleIndex !== undefined ? 'hls:' + selectedHlsSubtitleIndex : selectedSubtitleIndex !== undefined ? 'external:' + selectedSubtitleIndex : 'off'}
                    onChange={(e) => handleSubtitleTrackChange(e.target.value)}
                    className="min-w-0 w-full sm:w-[140px] bg-slate-800 text-xs font-semibold text-slate-100 outline-none disabled:cursor-not-allowed disabled:text-slate-500"
                    style={{ colorScheme: 'dark' }} title={tracksLoading ? 'Loading subtitles' : 'Subtitles'}>
                    <option value="off" className="bg-slate-900 text-slate-100">{tracksLoading ? 'Loading…' : 'Subtitles Off'}</option>
                    {hlsSubtitleTracks.map((track, index) => <option key={'hls-sub-' + track.index} value={'hls:' + track.index} className="bg-slate-900 text-slate-100">{formatTrackLabel(track, index, 'Subtitle')}</option>)}
                    {subtitleTracks.map((track, index) => <option key={'external-sub-' + track.index} value={'external:' + track.index} className="bg-slate-900 text-slate-100">{formatTrackLabel(track, index, 'Subtitle')}</option>)}
                  </select>
                </label>
              )}

              <div className="flex items-center bg-slate-800/80 rounded-lg p-0.5 text-xs font-medium text-slate-300 shrink-0">
                {[0.75, 1, 1.25, 1.5, 2].map((s) => (
                  <button key={s} onClick={() => handleSpeedChange(s)}
                    className={`px-2 py-1 rounded-md transition ${
                      playbackSpeed === s ? 'bg-cyan-500 text-slate-950 font-bold' : 'hover:text-white'
                    }`}>
                    {s}x
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>

    </div>
  );
};
