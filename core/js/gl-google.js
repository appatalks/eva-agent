// JavaScript
// For Google Generative Language API

// Google Gemini

function _geminiInputText(element) {
    if (!element) return '';
    if (typeof element.innerText === 'string') return element.innerText.trim();
    if (typeof element.textContent === 'string') return element.textContent.trim();
    return String(element.innerHTML || '')
        .replace(/<br\s*\/?\s*>/gi, '\n')
        .replace(/<[^>]+>/g, '')
        .trim();
}

function _geminiAppendText(output, className, label, text) {
    if (!output || !document.createElement) return;
    var line = document.createElement('div');
    line.className = className;
    var labelNode = document.createElement('span');
    labelNode.className = className;
    labelNode.textContent = label;
    var textNode = document.createElement('span');
    textNode.className = 'eva-safe-text';
    textNode.textContent = ' ' + String(text || '');
    textNode.style.whiteSpace = 'pre-wrap';
    line.appendChild(labelNode);
    line.appendChild(textNode);
    output.appendChild(line);
    output.scrollTop = output.scrollHeight;
}

function geminiSend(capturedEnvelope) {
    capturedEnvelope = capturedEnvelope || ((typeof captureRequestEnvelope === 'function')
        ? captureRequestEnvelope() : null);
    function requestIsCurrent() {
        return !!(capturedEnvelope && typeof isCurrentRequestEnvelope === 'function' &&
            isCurrentRequestEnvelope(capturedEnvelope));
    }
    if (!requestIsCurrent()) return;
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

    const sQuestion = _geminiInputText(document.getElementById("txtMsg"));
    if (!sQuestion) {
        alert("Type in your question!");
        txtMsg.focus();
        return;
    }

    getGoogleGlKey().then(GOOGLE_GL_KEY => {
        if (!requestIsCurrent()) return;
        document.getElementById("txtMsg").textContent = "";
        _geminiAppendText(document.getElementById("txtOutput"), 'user', 'You:', sQuestion);

    const geminiUrl = `https://generativelanguage.googleapis.com/v1alpha/models/gemini-2.0-flash-thinking-exp:generateContent?key=${GOOGLE_GL_KEY}`;

	const requestOptions = {
    	   method: "POST",
    	   headers: { "Content-Type": "application/json" },
    	   body: JSON.stringify({
               contents: geminiMessages.concat([
            	   { role: "user", parts: [{ text: sQuestion }] }
        	]),
        	systemInstruction: geminiMessages[0], // Assuming the first message is the system instruction
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
                if (!requestIsCurrent()) return;
                if (result.candidates[0].finishReason === "RECITATION") {
                    _geminiAppendText(
                        document.getElementById("txtOutput"), 'eva', 'Eva:',
                        'Sorry, please ask me another way.'
                    );
                } else { 
                    const candidate = result.candidates[0].content.parts;

                    // Extract thoughts and non-thoughts separately
                                        const thoughtsRaw = candidate.filter(part => part.thought).map(part => part.text).join("\n\n");
                                        const thoughts = (typeof canonicalizeEvaResponse === 'function')
                                            ? canonicalizeEvaResponse(thoughtsRaw).text : thoughtsRaw;
                    const nonThoughts = candidate.filter(part => !part.thought);

                    // Display main response via unified renderer
                    (async () => {
                        let mainResponse = nonThoughts.map(part => part.text).join("\n").trim();
                                                var canonicalResponse = canonicalizeEvaResponse(mainResponse, {
                                                    allowCamera: typeof _isExplicitCameraRequest === 'function' &&
                                                        _isExplicitCameraRequest(sQuestion)
                                                });
                                                mainResponse = canonicalResponse.text;
                                                if (typeof finalizeDirectProviderTurn === 'function') {
                                                        await finalizeDirectProviderTurn(
                                                            sQuestion, mainResponse, 'gemini', capturedEnvelope
                                                        );
                                                        if (!requestIsCurrent()) return;
                                                }
                                                if (thoughts) {
                                                        _geminiAppendText(
                                                            document.getElementById("txtOutput"),
                                                            'eva-thoughts', "Eva's Thoughts:", thoughts
                                                        );
                                                }
                        const out = document.getElementById("txtOutput");
                                                if (!await renderEvaResponse(
                                                    mainResponse, out, capturedEnvelope, [], canonicalResponse
                                                )) return;
                        if (!requestIsCurrent()) return;
                                                geminiMessages.push({ role: "user", parts: [{ text: sQuestion }] });
                                                geminiMessages.push({ role: "model", parts: [{ text: mainResponse }] });
                                                localStorage.setItem("geminiMessages", JSON.stringify(geminiMessages));
                        if (typeof saveCurrentSession === 'function') saveCurrentSession();
                    })();
                }
	    })
            .catch(error => {
                if (!requestIsCurrent()) return;
                console.error("Error:", error);
                _geminiAppendText(
                    document.getElementById("txtOutput"), 'error', 'Error:',
                    error && error.message ? error.message : String(error || 'Request failed')
                );
            });
    }).catch(error => {
        if (!requestIsCurrent()) return;
        console.error("Error:", error);
        _geminiAppendText(
            document.getElementById("txtOutput"), 'error', 'Error:',
            error && error.message ? error.message : String(error || 'Request failed')
        );
    });
}
