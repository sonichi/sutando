import { useCallback, useEffect, useRef, useState } from 'react';
import {
	fetchVisionState,
	postVisionFrame,
	postVisionStart,
	postVisionStop,
} from '@/lib/api';

/**
 * Browser-driven screen sharing into the active voice session (#735).
 *
 * The browser owns capture — `getDisplayMedia` gives the user the native
 * Chrome Tab / Window / Entire Screen picker — and this hook POSTs a
 * downscaled JPEG every ~1.5s to `/vision/frame` (proxied to the voice
 * agent). `enabled` should track whether voice is live; when it drops,
 * the hook tears the push session down.
 */

const FRAME_INTERVAL_MS = 1500;
const FRAME_W = 1280;
const FRAME_H = 720;
const FRAME_QUALITY = 0.6;
// Skip near-blank frames — getDisplayMedia paints a black frame for the
// first tick after a surface switch; a black JPEG just wastes context.
const MIN_FRAME_BYTES = 2048;
const STATE_POLL_MS = 2000;

export interface UseVisionResult {
	watching: boolean;
	frameCount: number;
	/** Live capture stream — feed into VisionPreview's <video>. */
	stream: MediaStream | null;
	toggleWatch: () => void;
}

export function useVision(enabled: boolean): UseVisionResult {
	const [watching, setWatching] = useState(false);
	const [frameCount, setFrameCount] = useState(0);
	const [stream, setStream] = useState<MediaStream | null>(null);

	// Offscreen <video> the capture loop draws from, a reused <canvas>, and
	// timer/stream handles. Refs — none of these should trigger a re-render.
	const videoRef = useRef<HTMLVideoElement | null>(null);
	const canvasRef = useRef<HTMLCanvasElement | null>(null);
	const frameTimer = useRef<number | null>(null);
	const streamRef = useRef<MediaStream | null>(null);
	const rearmedRef = useRef(false);

	const teardown = useCallback(() => {
		if (frameTimer.current != null) {
			window.clearInterval(frameTimer.current);
			frameTimer.current = null;
		}
		if (streamRef.current) {
			streamRef.current.getTracks().forEach((t) => t.stop());
			streamRef.current = null;
		}
		if (videoRef.current) videoRef.current.srcObject = null;
		rearmedRef.current = false;
		setStream(null);
		setWatching(false);
		setFrameCount(0);
	}, []);

	const captureFrame = useCallback(() => {
		const video = videoRef.current;
		if (!video || video.readyState < 2 || !video.videoWidth) return;
		let canvas = canvasRef.current;
		if (!canvas) {
			canvas = document.createElement('canvas');
			canvasRef.current = canvas;
		}
		canvas.width = FRAME_W;
		canvas.height = FRAME_H;
		const ctx = canvas.getContext('2d');
		if (!ctx) return;
		ctx.drawImage(video, 0, 0, FRAME_W, FRAME_H);
		canvas.toBlob(
			(blob) => {
				if (!blob || blob.size < MIN_FRAME_BYTES) return;
				void postVisionFrame(blob)
					.then((res) => {
						if (res.ok) {
							setFrameCount((n) => n + 1);
							rearmedRef.current = false;
							return;
						}
						// 409 → the server fell out of push mode (voice-agent
						// restart). Re-arm once without re-prompting the user.
						if (res.status === 409 && !rearmedRef.current) {
							rearmedRef.current = true;
							void postVisionStart();
						}
					})
					.catch(() => {
						/* network blip — the next tick retries */
					});
			},
			'image/jpeg',
			FRAME_QUALITY
		);
	}, []);

	const startWatch = useCallback(async () => {
		const md = navigator.mediaDevices;
		if (!md?.getDisplayMedia) return;
		let s: MediaStream;
		try {
			s = await md.getDisplayMedia({
				video: { width: FRAME_W, height: FRAME_H, frameRate: 2 },
				audio: false,
			});
		} catch {
			return; // user cancelled the picker
		}
		streamRef.current = s;
		setStream(s);

		let video = videoRef.current;
		if (!video) {
			video = document.createElement('video');
			video.muted = true;
			video.playsInline = true;
			videoRef.current = video;
		}
		video.srcObject = s;
		void video.play().catch(() => {});

		// The user can end sharing from Chrome's native "Stop sharing" bar.
		s.getVideoTracks()[0]?.addEventListener('ended', () => {
			teardown();
			void postVisionStop();
		});

		const resp = await postVisionStart().catch(() => ({ status: 'failed' as const }));
		if (resp.status === 'failed') {
			teardown();
			return;
		}
		setWatching(true);
		setFrameCount(0);
		if (frameTimer.current != null) window.clearInterval(frameTimer.current);
		// First capture slightly delayed so the video has painted a frame.
		window.setTimeout(captureFrame, 300);
		frameTimer.current = window.setInterval(captureFrame, FRAME_INTERVAL_MS);
	}, [captureFrame, teardown]);

	const stopWatch = useCallback(async () => {
		teardown();
		await postVisionStop();
	}, [teardown]);

	const toggleWatch = useCallback(() => {
		if (watching) void stopWatch();
		else void startWatch();
	}, [watching, startWatch, stopWatch]);

	// While voice is live, poll the server state — if it reports no stream
	// but we still hold one, our push session is stale; tear it down. When
	// voice ends, drop the whole session.
	useEffect(() => {
		if (!enabled) {
			teardown();
			return;
		}
		const id = window.setInterval(() => {
			void fetchVisionState()
				.then((st) => {
					if (!st.streaming && streamRef.current) teardown();
				})
				.catch(() => {});
		}, STATE_POLL_MS);
		return () => window.clearInterval(id);
	}, [enabled, teardown]);

	// Teardown on unmount.
	useEffect(() => () => teardown(), [teardown]);

	return { watching, frameCount, stream, toggleWatch };
}
