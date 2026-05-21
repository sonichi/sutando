import DOMPurify from 'dompurify';
import { marked } from 'marked';

/**
 * Render chat message text as sanitized HTML.
 *
 * Chat content is UNTRUSTED: assistant replies, tool output, and anything
 * the agent echoes back can carry arbitrary markup. This is deliberately
 * separate from lib/markdown.ts (notes — the user's own trusted filesystem,
 * escaped but not sanitized). Every chat result goes through DOMPurify
 * before it can reach `dangerouslySetInnerHTML`.
 */

// Open links in a new tab without handing the new context a live
// `window.opener` reference. Registered once at module load.
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
	if (node.tagName === 'A') {
		node.setAttribute('target', '_blank');
		node.setAttribute('rel', 'noopener noreferrer');
	}
});

marked.setOptions({ gfm: true, breaks: true });

export const renderChatMarkdown = (text: string): string => {
	const rawHtml = marked.parse(text, { async: false }) as string;
	return DOMPurify.sanitize(rawHtml, { ADD_ATTR: ['target'] });
};
