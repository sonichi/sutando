// voice-core/recording.ts — the controlled export of the conversation-store
// recording API for voice-surface plugins.
//
// The sqlite ENGINE (open/init/prepared-stmt machinery, sessions +
// session_events tables, cross-surface views) stays in src/conversation-store
// — it has four in-repo consumers beyond voice. What plugins get through this
// module is exactly the surface API: record rows, register their own table
// DDL at startup (registerSurfaceTable — the engine never names plugins), and
// the role/kind helpers used to keep cross-surface SQL working on one DB file.
//
// Round-④ note: the discord_voice DDL currently still lives in the store's
// init() (move-intact rounds keep behavior identical). When the skill is
// deleted from sutando, that DDL moves to the plugin's startup
// registerSurfaceTable call — same table name, same file, no data migration.

export {
	recordConversation,
	recordEvent,
	recordSession,
	recordToolCall,
	registerSurfaceTable,
	sourceFromRole,
	kindFromRole,
} from '../conversation-store.js';
