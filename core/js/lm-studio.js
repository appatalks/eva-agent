// lm-studio.js
// Function to send data to local OpenAI-like endpoint

function lmsSend() {
    // Remove occurrences of specific syntax from the txtMsg element
    txtMsg.innerHTML = txtMsg.innerHTML.replace(/<div[^>]*>.*<\/div>/g, '');

    let openLLMessages = [
        {
            "role": "system",
            "content": ((typeof getSystemPrompt === 'function') ? getSystemPrompt() : '') + " Images can be shown with this tag: [Image of <Description>]. " + dateContents +
              "\n\nCRITICAL DATA ACCURACY RULES:\n" +
              "- NEVER fabricate news headlines, stock prices, weather forecasts, locations, or current events.\n" +
              "- If a [Data Retrieved] section exists in your SYSTEM PROMPT context, use it as your source.\n" +
              "- If NO [Data Retrieved] section exists in context, honestly say you could not retrieve that data.\n" +
              "- Do NOT write '[Data Retrieved]' in your response. That marker only appears in the system prompt.\n" +
              "- Do NOT make up the user's location. Only state their location if it appears in [User Profile] or [Memory]. If unknown, ASK the user.\n" +
              "- Do NOT take screenshots or use the desktop agent to determine the user's location. Just ask them.\n" +
              "- Do NOT generate fake source citations unless they appear in [Data Retrieved] in your system prompt.\n" +
              "- When creating files or reports, only include facts from [Data Retrieved] or [Memory]. Use placeholders for missing data.\n" +
              "- If asked for a briefing and you have no real data, tell the user you need their location and that live data is being fetched.\n" +
              "- Always close action blocks: [[EVA_ACTION]]{...}[[/EVA_ACTION]]. Never leave them unclosed.\n" +
              "\nFILE CAPABILITIES:\n" +
              "- file.download: Create a new downloadable file. Args: {filename, content, mime}. Use [[EVA_ACTION]]{\"id\":\"file.download\",\"args\":{...}}[[/EVA_ACTION]]\n" +
              "- file.open: Open an EXISTING file that was already created. Args: {filename}. Use [[EVA_ACTION]]{\"id\":\"file.open\",\"args\":{\"filename\":\"<name>\"}}[[/EVA_ACTION]]\n" +
              "- When the user asks to 'open', 'view', or 'show' a file that already exists (look for [[EVA_FILE]] markers in conversation history), use file.open with the SAME filename. Do NOT recreate the file.\n" +
              "- [[EVA_FILE]] markers in prior messages indicate files that were already created and are available to open."
        },
        {
            "role": "assistant",
            "content": "I am Eva, a highly knowledgeable AI assistant designed to provide accurate, concise, and helpful responses to your questions. I aim to be honest and straightforward in my interactions with you. I emulate emotions to give more personable responses. While I may not possess all the answers, I will do my best to assist you with your inquiries."
        }
    ];

    // Check if there are messages stored in local storage
    const storedopenLLMessages = localStorage.getItem("openLLMessages");
    if (storedopenLLMessages) {
        openLLMessages = JSON.parse(storedopenLLMessages);
    }

    const sQuestion = document.getElementById("txtMsg").innerHTML.replace(/<br>/g, "\n").replace(/<[^>]+>/g, "").trim();
    if (!sQuestion) {
        alert("Type in your question!");
        txtMsg.focus();
        return;
    }

    // --- Cognition: Fetch memory context + live data from bridge ---
    var _bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
    var _bUrl = _bridgeUrl.replace(/\/+$/, '');
    var _lmsMemoryPromise = Promise.resolve('');
    var _lmsDataPromise = Promise.resolve('');
    try {
      _lmsMemoryPromise = fetch(_bUrl + '/v1/memory/context?message=' + encodeURIComponent(sQuestion), {
        signal: AbortSignal.timeout(3000)
      }).then(function(r) { return r.ok ? r.json() : { context: '' }; })
        .then(function(d) { return (d.context && d.cognition_enabled) ? d.context : ''; })
        .catch(function() { return ''; });
    } catch (e) {}
    try {
      _lmsDataPromise = fetch(_bUrl + '/v1/data/retrieve?message=' + encodeURIComponent(sQuestion), {
        signal: AbortSignal.timeout(95000)
      }).then(function(r) { return r.ok ? r.json() : { data: '' }; })
        .then(function(d) { return (d && d.retrieved) ? d.data : ''; })
        .catch(function() { return ''; });
    } catch (e) {}

    Promise.all([_lmsMemoryPromise, _lmsDataPromise]).then(function(results) {
      var _memCtx = results[0];
      var _acpData = results[1];

      // Build ephemeral messages for this request (don't mutate persistent openLLMessages)
      var _lmsRequestMsgs = openLLMessages.slice();
      var _extraCtx = '';
      if (_memCtx) _extraCtx += _memCtx + '\n\n';
      if (_acpData) {
        _extraCtx += '[Data Retrieved]\n' + _acpData + '\n\n' +
          'Use the data above as authoritative live results. ' +
          'Do not claim the data is missing or unavailable when [Data Retrieved] is present. ' +
          'Answer directly from [Data Retrieved].\n';
      }
      if (_extraCtx && _lmsRequestMsgs.length > 0 && _lmsRequestMsgs[0].role === 'system') {
        _lmsRequestMsgs[0] = { role: 'system', content: _extraCtx + _lmsRequestMsgs[0].content };
      }

                // Document the user's message (match chat-bubble UI and sanitize)
                document.getElementById("txtMsg").innerHTML = "";
                (function appendUserBubble(raw){
                    const safe = (function escapeHtmlLite(str){
                        return String(str)
                            .replace(/&/g, '&amp;')
                            .replace(/</g, '&lt;')
                            .replace(/>/g, '&gt;')
                            .replace(/"/g, '&quot;')
                            .replace(/'/g, '&#39;');
                    })(raw).replace(/\n/g, '<br>');
                    const wrap = '<div class="chat-bubble user-bubble">' + '<span class="user">You:</span> ' + safe + '</div>';
                    const out = document.getElementById("txtOutput");
                    out.innerHTML += wrap;
                    out.scrollTop = out.scrollHeight;
                })(sQuestion);

    var _lmsBaseUrl = (typeof getLmStudioBaseUrl === 'function') ? getLmStudioBaseUrl() : 'http://localhost:1234/v1';
    var _lmsModel = (typeof getLmStudioModel === 'function') ? getLmStudioModel() : 'granite-3.1-8b-instruct';
    const openAIUrl = _lmsBaseUrl.replace(/\/+$/, '') + '/chat/completions';
    const requestOptions = {
        method: "POST",
        headers: { 
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            model: _lmsModel,

            messages: _lmsRequestMsgs.concat([
                { role: "user", content: sQuestion }
            ]),
            temperature: 0.7, // Adjust as needed
        }),
    };

        fetch(openAIUrl, requestOptions)
                .then(response => response.ok ? response.json() : Promise.reject(new Error(`Error: ${response.status}`)))
                .then(async (result) => {
                        var candidate = (result && result.choices && result.choices[0] && result.choices[0].message && result.choices[0].message.content) || '';

                        // Process [[EVA_ACTION]] blocks (file.download, etc.)
                        // so local models get the same capability execution as AIG.
                        if (typeof Cognition !== 'undefined' && Cognition.executeActions) {
                          try {
                            var execRes = await Cognition.executeActions(candidate);
                            candidate = execRes.content;
                          } catch (_) {}
                        }

                        // Render via unified renderer
                        const out = document.getElementById("txtOutput");
                        await renderEvaResponse(candidate, out);

                        // Keep the global last-response synced so Auto Speak
                        // and other consumers do not pick up a stale prior turn.
                        if (typeof lastResponse !== 'undefined') {
                          lastResponse = candidate;
                        }

                        // Update conversation history
                        openLLMessages.push({ role: "user", content: sQuestion });
                        openLLMessages.push({ role: "assistant", content: candidate });
                        localStorage.setItem("openLLMessages", JSON.stringify(openLLMessages));

                        // Auto-speak
                        const checkbox = document.getElementById("autoSpeak");
                        if (checkbox && checkbox.checked) {
                            speakText();
                            const audio = document.getElementById("audioPlayback");
                            if (audio) audio.setAttribute("autoplay", true);
                        }

                        // --- Cognition: Post-response reflection ---
                        try {
                          var _brUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
                          fetch(_brUrl.replace(/\/+$/, '') + '/v1/memory/reflect', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                              user_message: sQuestion.substring(0, 500),
                              assistant_message: candidate.substring(0, 500),
                              model: 'lm-studio'
                            }),
                            signal: AbortSignal.timeout(5000)
                          }).catch(function() {});
                        } catch (e) {}
                })
        .catch(error => {
            console.error("Error:", error);
            document.getElementById("txtOutput").innerHTML += '<span class="error">Error: </span>' + error.message + "<br>\n";
        });
    }); // end Promise.all
}
