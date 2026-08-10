// JavaScript
// For Google Generative Language API

// Google Gemini

function geminiSend() {
    // Remove occurrences of specific syntax from the txtMsg element
    txtMsg.innerHTML = txtMsg.innerHTML.replace(/<div[^>]*>.*<\/div>/g, '');

    function getGoogleGlKey() {
        // Prefer local inline config if present
        if (typeof window !== 'undefined' && window.__LOCAL_CONFIG__ && window.__LOCAL_CONFIG__.GOOGLE_GL_KEY) {
            return Promise.resolve(window.__LOCAL_CONFIG__.GOOGLE_GL_KEY);
        }
        // If options.js loaded config already, use global variable
        if (typeof GOOGLE_GL_KEY !== 'undefined' && GOOGLE_GL_KEY) {
            return Promise.resolve(GOOGLE_GL_KEY);
        }
        // Fallback to config.json (requires http(s) server)
        return fetch('./config.json')
            .then(r => r.ok ? r.json() : Promise.reject(new Error('Missing config.json')))
            .then(cfg => cfg.GOOGLE_GL_KEY);
    }

    let geminiMessages = [
        {
            "role": "user",
            "parts": [
                {
                    "text": ((typeof getSystemPrompt === 'function') ? getSystemPrompt() : '') + " When you are asked to show an image, instead describe the image with [Image of <Description>]. " + dateContents
                }
            ]
        },
        {
            "role": "model",
            "parts": [
                {
                    "text": "I am Eva, a highly knowledgeable AI assistant designed to provide accurate, concise, and helpful responses to your questions. I aim to be honest and straightforward in my interactions with you. I emulate emotions to give more personable responses. While I may not possess all the answers, I will do my best to assist you with your inquiries."
                }
            ]
        }
    ];

    // Check if there are messages stored in local storage
    const storedGeminiMessages = localStorage.getItem("geminiMessages");
    if (storedGeminiMessages) {
        geminiMessages = JSON.parse(storedGeminiMessages);
    }

    const sQuestion = document.getElementById("txtMsg").innerHTML.replace(/<br>/g, "\n").replace(/<[^>]+>/g, "").trim();
    if (!sQuestion) {
        alert("Type in your question!");
        txtMsg.focus();
        return;
    }
    const signalContext = (typeof captureSignalDeliveryContext === 'function')
        ? captureSignalDeliveryContext(sQuestion)
        : null;
    const sessionId = (typeof ensureActiveSessionId === 'function')
        ? ensureActiveSessionId() : ((typeof _activeSessionId === 'function') ? (_activeSessionId() || '') : '');
    const geminiPromptBudget = EvaPromptBudget.compactGeminiContents(
        geminiMessages.concat([{ role: "user", parts: [{ text: sQuestion }] }]),
        { budget: 10000, recentTurns: 6, pinnedIndexes: [0] }
    );
    let geminiMemoryContextPromise = Promise.resolve('');
    try {
        const bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
        geminiMemoryContextPromise = fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/context?message=' + encodeURIComponent(sQuestion) + '&session_id=' + encodeURIComponent(sessionId), {
            signal: AbortSignal.timeout(3000)
        }).then(response => response.ok ? response.json() : { context: '' })
            .then(data => (data.context && data.cognition_enabled) ? data.context : '')
            .catch(() => '');
    } catch (e) {}

    Promise.all([getGoogleGlKey(), geminiMemoryContextPromise]).then(([GOOGLE_GL_KEY, geminiMemoryContext]) => {
        document.getElementById("txtMsg").innerHTML = "";
        document.getElementById("txtOutput").innerHTML += '<span class="user">You: </span>' + escapeHtml(sQuestion).replace(/\n/g, '<br>') + "<br>\n";

    const geminiUrl = `https://generativelanguage.googleapis.com/v1alpha/models/gemini-2.0-flash-thinking-exp:generateContent?key=${GOOGLE_GL_KEY}`;
    let systemInstruction = geminiPromptBudget.messages[0];
    if (geminiMemoryContext && systemInstruction && Array.isArray(systemInstruction.parts)) {
        const baseText = systemInstruction.parts.map(part => part.text || '').join("\n");
        systemInstruction = {
            role: systemInstruction.role || "user",
            parts: [{ text: geminiMemoryContext + "\n\n" + baseText }]
        };
    }

	const requestOptions = {
    	   method: "POST",
    	   headers: { "Content-Type": "application/json" },
    	   body: JSON.stringify({
               contents: geminiPromptBudget.messages,
				systemInstruction: systemInstruction,
        	generationConfig: {
            	    temperature: 0.7, 
            	    // maxOutputTokens: 1024, 
            	    responseMimeType: "text/plain",
            	    thinking_config: { include_thoughts: true } // Enable thinking
        	}
    	   }),
	};

    fetch(geminiUrl, requestOptions)
            .then(response => response.ok ? response.json() : Promise.reject(new Error(`Error: ${response.status}`))) // Updated Error handling
            .then(result => {
                if (result.candidates[0].finishReason === "RECITATION") {
                    document.getElementById("txtOutput").innerHTML += '<span class="eva">Eva: Sorry, please ask me another way.</span><br>\n';
                } else { 
                    const candidate = result.candidates[0].content.parts;

                    // Extract thoughts and non-thoughts separately
                    const thoughts = candidate.filter(part => part.thought).map(part => part.text).join("\n\n");
                    const nonThoughts = candidate.filter(part => !part.thought);

                    // Display thoughts (if any)
                    if (thoughts) {
                        document.getElementById("txtOutput").innerHTML += '<span class="eva-thoughts">Eva\'s Thoughts:</span><br>' + escapeHtml(thoughts).replace(/\n/g, '<br>') + "<br><br>\n";
                    }

                    // Display main response via unified renderer
                    (async () => {
                        let mainResponse = nonThoughts.map(part => part.text).join("\n").trim();
                        const out = document.getElementById("txtOutput");
                        await renderEvaResponse(mainResponse, out, {
                            signalAuthorized: !!(signalContext && signalContext.authorized),
                            signalMessage: signalContext ? signalContext.message : '',
                            signalRequest: sQuestion,
                            signalContext: signalContext
                        });
                        if (mainResponse) {
                            try {
                                const bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
                                fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/reflect', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({
                                        user_message: sQuestion.substring(0, 500),
                                        assistant_message: mainResponse.substring(0, 500),
                                        model: 'gemini-2.0-flash-thinking-exp',
                                        session_id: sessionId
                                    }),
                                    signal: AbortSignal.timeout(5000)
                                }).catch(() => {});
                            } catch (e) {}
                        }
                    })();

                    // Update conversation history: log both thoughts and non-thoughts
                    geminiMessages.push({ role: "user", parts: [{ text: sQuestion }] });
                    geminiMessages.push({ role: "model", parts: [...candidate] }); // Log the entire candidate
                    localStorage.setItem("geminiMessages", JSON.stringify(geminiMessages));
                }
	    })
            .catch(error => {
                console.error("Error:", error);
                document.getElementById("txtOutput").innerHTML += '<span class="error">Error: </span>' + escapeHtml(error.message) + "<br>\n";
            });
    });
}
