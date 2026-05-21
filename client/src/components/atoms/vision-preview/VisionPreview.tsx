import { useEffect, useRef } from 'react';

export interface VisionPreviewProps {
	/** Capture stream from useVision — null when not sharing. */
	stream: MediaStream | null;
	frameCount: number;
}

/**
 * Fixed bottom-right preview of what Sutando is seeing. Mirrors the
 * MediaStream captured by getDisplayMedia so the user can verify the
 * picked surface and watch the same frames the model receives.
 */
export default function VisionPreview({ stream, frameCount }: VisionPreviewProps) {
	const videoRef = useRef<HTMLVideoElement | null>(null);

	useEffect(() => {
		const el = videoRef.current;
		if (el) el.srcObject = stream;
	}, [stream]);

	if (!stream) return null;

	return (
		<div className="fixed right-4 bottom-4 z-[200] w-[280px] rounded-[10px] border-2 border-amber-400 bg-black/60 p-1.5 shadow-[0_4px_18px_rgba(0,0,0,0.35)]">
			<div className="mb-1 flex items-center justify-between text-[11px] text-amber-400">
				<span>👁️ Sutando is seeing</span>
				<span className="text-[10px] text-neutral-300">
					{frameCount} frame{frameCount === 1 ? '' : 's'}
				</span>
			</div>
			<video
				ref={videoRef}
				autoPlay
				muted
				playsInline
				className="block max-h-[180px] w-full rounded-md bg-black"
			/>
		</div>
	);
}
