// One-off: join each voice channel briefly and clear its voice-channel status.
// The Set-Voice-Channel-Status API requires the bot to be IN the channel, so a
// stale '👁 seeing my local screen' status left by a crashed/killed discord-voice
// session can only be cleared by rejoining. Joins via the gateway voice-state
// update (no audio), clears status, leaves. Run on a host whose bot token has
// access to the channels. Usage: node clear-vc-status.mjs
import { Client, GatewayIntentBits } from 'discord.js';
import { joinVoiceChannel } from '@discordjs/voice';

const TOKEN = process.env.DISCORD_BOT_TOKEN;
const GUILD = '1485653766404444352';
const CHANNELS = [
  ['1485653767402553461', 'Lounge'],
  ['1485653767402553462', 'Stream Room'],
  ['1505592652802818169', 'echo act iv'],
];

if (!TOKEN) { console.error('no DISCORD_BOT_TOKEN'); process.exit(1); }

const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates] });

client.once('ready', async () => {
  console.log('logged in as', client.user.tag);
  const g = await client.guilds.fetch(GUILD);
  for (const [ch, name] of CHANNELS) {
    let conn;
    try {
      conn = joinVoiceChannel({ channelId: ch, guildId: GUILD, adapterCreator: g.voiceAdapterCreator, selfDeaf: true, selfMute: true });
      await new Promise(r => setTimeout(r, 4000)); // let the gateway register the voice state
      const res = await fetch(`https://discord.com/api/v10/channels/${ch}/voice-status`, {
        method: 'PUT',
        headers: { Authorization: `Bot ${TOKEN}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: '' }),
      });
      console.log(`[${name} ${ch}] clear status -> ${res.status} ${res.status >= 400 ? await res.text() : ''}`);
    } catch (e) {
      console.log(`[${name} ${ch}] err: ${e.message}`);
    } finally {
      try { conn && conn.destroy(); } catch {}
      await new Promise(r => setTimeout(r, 1200)); // flush leave frame
    }
  }
  process.exit(0);
});

client.login(TOKEN).catch(e => { console.error('login failed', e.message); process.exit(1); });
