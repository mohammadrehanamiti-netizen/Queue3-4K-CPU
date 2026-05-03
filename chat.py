class Chat:

    START_TEXT = """👋 <b>Welcome to the Premium Encoding Server!</b>  

I am a high-performance Telegram bot designed to Hard-Mux, Soft-Mux, and Encode videos with precision.

🚀 <b>Core Capabilities:</b>
• Custom Encoding Settings (Res, FPS, Codec)
• Live Queue & Progress Tracking
• Support for Direct URLs & M3U8 Streams
• High-Speed Processing

Tap /help to see how to use my features!

💡 <b>Developer:</b> @THe_vK_3
"""

    HELP_USER = "🤖 How can I assist you?"

    HELP_TEXT = """🛠 <b>How to Use the Encoding Server</b>

<b>1️⃣ Configure (Optional)</b>
Send <code>/settings</code> to set your default Resolution, FPS, Codec, and CRF.

<b>2️⃣ Send Media</b>
• Send a Video file or Direct Link.
• (Optional) Send a Subtitle file (<code>.srt</code> or <code>.ass</code>).

<b>3️⃣ Choose a Mode</b>
• <code>/softmux</code> - Embeds subs (toggleable in players).
• <code>/hardmux</code> - Burns subs directly into the video.
• <code>/nosub</code> - Standard video encoding/compression.
• <code>/m3u8 [url] [name]</code> - Download & encode streams.

<b>4️⃣ Manage Queue</b>
• <code>/status</code> - View live processing and pending jobs.
• <code>/cancel [job_id]</code> - Abort a specific task.

⚠️ <i>Note: Hardmux currently works best with standard English fonts.</i>  

🎬 <b>For Donghua watching, Visit:</b> <a href="https://fackyhindidonghuas.in/">Facky Hindi Donghua</a>  
💡 <b>Developer:</b> @THe_vK_3
"""

    NO_AUTH_USER = """🚫 <b>Access Denied!</b>  

You are not authorized to use this private encoding server.  
📩 Contact @THe_vK_3 to request access.
"""

    DOWNLOAD_SUCCESS = """✅ <b>File Downloaded Successfully!</b>  
⏱️ <b>Time Taken:</b> {}s  
"""

    RENAME_PROMPT = """📝 <b>Rename Output File</b>

📁 <b>Current Name:</b>
<code>{}</code>

💬 Send the new filename <b>with extension</b> (e.g., <code>Video_4K.mp4</code>).
👉 Or simply reply with <code>default</code> to keep the original name.
"""

    FILE_SIZE_ERROR = "❌ <b>ERROR:</b> Unable to extract file size from the URL!\n\n💡 <b>Credits:</b> @Cybrion"
    
    MAX_FILE_SIZE = "⚠️ <b>File too Large!</b>\nThe maximum file size allowed by Telegram is <b>2GB</b>."
    
    LONG_CUS_FILENAME = """⚠️ <b>Filename Too Long!</b>  
The filename you provided exceeds 60 characters. Please send a shorter name.
"""

    UNSUPPORTED_FORMAT = """❌ <b>Unsupported Format!</b>
The format <b>{}</b> is not recognized by the encoding server.

📁 <b>File:</b> <code>{}</code>
"""
