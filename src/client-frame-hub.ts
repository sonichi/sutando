// Client-frame hub: optional voice plugins register handlers; the host offers them
// every client JSON frame it does not own. One bad handler never reaches the others.

export type ClientFrame = Record<string, unknown>;
export type ClientFrameHandler = (frame: ClientFrame) => boolean | void | Promise<boolean | void>;
export type ClientDisconnectedHandler = () => void;

export interface ClientFrameHub {
	onClientFrame(handler: ClientFrameHandler): void;
	onClientDisconnected(handler: ClientDisconnectedHandler): void;
	/** Offer a frame to every handler. -> true when one claimed it synchronously. */
	dispatch(frame: unknown): boolean;
	disconnected(): void;
}

export function createClientFrameHub(
	log: (msg: string, detail?: unknown) => void = (m, d) => console.error(m, d),
): ClientFrameHub {
	const frameHandlers: ClientFrameHandler[] = [];
	const disconnectHandlers: ClientDisconnectedHandler[] = [];
	return {
		onClientFrame(handler) {
			if (typeof handler === 'function') frameHandlers.push(handler);
		},
		onClientDisconnected(handler) {
			if (typeof handler === 'function') disconnectHandlers.push(handler);
		},
		dispatch(frame) {
			if (!frame || typeof frame !== 'object' || Array.isArray(frame)) return false;
			let claimed = false;
			for (const handler of frameHandlers) {
				try {
					const out = handler(frame as ClientFrame);
					if (out === true) claimed = true;
					else if (out && typeof (out as PromiseLike<unknown>).then === 'function') {
						Promise.resolve(out).then(undefined, (err: unknown) => {
							log('[client-frame] async handler rejected:', err instanceof Error ? err.message : err);
						});
					}
				} catch (err) {
					log('[client-frame] handler threw:', err instanceof Error ? err.message : err);
				}
			}
			return claimed;
		},
		disconnected() {
			for (const handler of disconnectHandlers) {
				try {
					handler();
				} catch (err) {
					log('[client-frame] disconnect handler threw:', err instanceof Error ? err.message : err);
				}
			}
		},
	};
}
