// Javascript
// For OpenAI API

// API Call for latest gpt classes
async function trboSend(capturedEnvelope) {
  capturedEnvelope = capturedEnvelope || ((typeof captureRequestEnvelope === 'function')
    ? captureRequestEnvelope() : null);
  function requestIsCurrent() {
    return !!(capturedEnvelope && typeof isCurrentRequestEnvelope === 'function' &&
      isCurrentRequestEnvelope(capturedEnvelope));
  }
  if (!requestIsCurrent()) return;

  // Remove occurrences of the specific syntax from the txtMsg element
	txtMsg.innerHTML = txtMsg.innerHTML.replace(/<img\b[^>]*>/g, '');

  var sQuestion = txtMsg.innerHTML;
    sQuestion = sQuestion.replace(/<br>/g, "\n");
  if (sQuestion.trim() == "") {
    alert("Type in your question!");
    txtMsg.focus();
    return;
  }

  var _requestHeaders = {};
  var oHttp = {
    readyState: 0, status: 0, responseText: '', onreadystatechange: null,
    method: 'POST', url: '',
    open: function(method, url) { this.method = method; this.url = url; },
    setRequestHeader: function(name, value) { _requestHeaders[name] = value; },
    send: async function(body) {
      try {
        var response = await fetch(this.url, {
          method: this.method, headers: _requestHeaders, body: body,
          redirect: 'error', credentials: 'omit'
        });
        this.status = response.status;
        this.responseText = response.ok ? await response.text() : '';
      } catch (error) {
        this.status = 0;
        this.responseText = JSON.stringify({
          error: { message: 'provider request failed' }
        });
      }
      this.readyState = 4;
      if (typeof this.onreadystatechange === 'function') {
        await this.onreadystatechange();
      }
    }
  };
  oHttp.open("POST", "https://api.openai.com/v1/chat/completions");
    oHttp.setRequestHeader("Accept", "application/json");
    oHttp.setRequestHeader("Content-Type", "application/json");
    oHttp.setRequestHeader("Authorization", "Bearer " + OPENAI_API_KEY)

    // Error Handling - Needs more testing
    oHttp.onreadystatechange = async function () {
        if (oHttp.readyState === 4) {
        if (!requestIsCurrent()) return;
    	  // Check for errors
    	  if (oHttp.status === 500) {
      txtOutput.innerHTML += "<br> Error 500: Internal Server Error<br>";
      	    console.log("Error 500: Internal Server Error gpt-core.js");
      	    return;
    	  }
    	  if (oHttp.status === 429) {
          txtOutput.innerHTML += "<br> Error 429: Too Many Requests<br>";
            console.log("Error 429: Too Many Requests gpt-core.js");
      	    return;
    	  }
          if (oHttp.status === 404) {
            txtOutput.innerHTML += "<br> Error 404: Not Found<br>";
            console.log("Error 404: Not Found gpt-core.js");
            return;
          }
          if (oHttp.status === 400) {
            txtOutput.innerHTML += "<br> Error 400: Invalid Request<br>";
            console.log("Error 400: Invalid Request gpt-core.js");
            return;
          }
            //console.log(oHttp.status);
            var oJson = {}
            try {
                oJson = JSON.parse(oHttp.responseText);  // API Response Data
            } catch (ex) {
                txtOutput.innerHTML += "Error: " + escapeHtml(ex.message);
		console.log("Error: gpt-core.js JSON parse");
		return;
              }
	
	// EasterEgg
  var usage = (oJson.usage && typeof oJson.usage === 'object') ? oJson.usage : {};
  if ((usage.completion_tokens === 420) || (usage.total_tokens === 420)) {
          function displayImage() {
	      var image = document.getElementById("eEgg");
	      image.style.display = "flex";
	      setTimeout(function() {
	        image.style.opacity = 1;
  	      }, 50);
  	      setTimeout(function() {
    		image.style.opacity = 0;
  	      }, 2420);
  	      setTimeout(function() {
    		image.style.display = "none";
  	      }, 2920);
           }
          displayImage();
        }
	
	// Timeout Error Exponetial Backoff 
        if (oJson.error && oJson.error.message) {
	    if (oJson.error.message == "overloaded" && retryCount < maxRetries) {
                retryCount++;
                var retryDelay = Math.pow(2, retryCount) * 1000;
                console.log("Too busy. Retrying in " + retryDelay + "ms");
                setTimeout(function() {
                  if (requestIsCurrent()) trboSend(capturedEnvelope);
                }, retryDelay);
                return;
            }
	    else {
                txtOutput.innerHTML += "Error Other: " + escapeHtml(oJson.error.message);
	        console.log("Error Other: gpt-core.js Line 89");
                retryCount = 0;	  
	    }
       	}
	
	// Interpret AI Response after Error Handling
  else if (Array.isArray(oJson.choices) && oJson.choices[0] && oJson.choices[0].message) {
	    // console.log("gpt-core.js Line 96" + oJson.choices + "" + oJson.choices[0].message);
	    // Always Run Response 
            var s = oJson.choices[0].message;
      var canonicalResponse = (typeof canonicalizeEvaResponse === 'function')
        ? canonicalizeEvaResponse(s.content, {
            allowCamera: typeof _isExplicitCameraRequest === 'function' &&
              _isExplicitCameraRequest(sQuestion)
          })
        : { text: String(s.content || '') };
      var cleanedContent = canonicalResponse.text;
      if (typeof finalizeDirectProviderTurn === 'function') {
        await finalizeDirectProviderTurn(
          sQuestion, cleanedContent, sModel, capturedEnvelope
        );
        if (!requestIsCurrent()) return;
      }
	    // Empty Response Handling / Render via unified renderer
      if (!await renderEvaResponse(
        cleanedContent, txtOutput, capturedEnvelope, [], canonicalResponse
      )) return;
            if (!requestIsCurrent()) return;
       	
            // Send to Local Storage - possibly way to intigrate into memory
	    let outputWithoutTags = txtOutput.innerText + "\n";
	    masterOutput += outputWithoutTags;
	    localStorage.setItem("masterOutput", masterOutput);
	    
	    // Set lastResponse
      lastResponse = cleanedContent + "\n";
            // console.log("gpt-core.js Line 152" + lastResponse);

            }            
        }

  	// Check the state of the checkbox and have fun
     if (!requestIsCurrent()) return;
     const checkbox = document.getElementById("autoSpeak");
	   if (checkbox && checkbox.checked) {
	     speakText();
    	     const audio = document.getElementById("audioPlayback");
	     audio.setAttribute("autoplay", true);
	   }
    };

    // payload parameters
    var sModel = selModel.value; 
    var iMaxTokens = (typeof getModelMaxTokens === 'function') ? getModelMaxTokens() : 4096;
  var dTemperature = (typeof getModelTemperature === 'function') ? getModelTemperature() : 0.7; 
    var eFrequency_penalty = 0.0; 
    var cPresence_penalty = 0.0; 
    var hStop = "&*&"; 
  var topP = 1.0; // Optional nucleus sampling

  // Model flags
  var isGpt5 = (sModel && sModel.indexOf('gpt-5') === 0);
  var isLatest = (sModel === 'latest');
  var isO1 = (sModel && sModel.indexOf('o1') === 0);

    // Messages payload
    // Check if the messages item exists in localStorage
    if (!localStorage.getItem("messages")) {
      // If it does not exist, create an array with the initial messages
      const iMessages = [
        { role: 'developer', content: 'You are Eva. You have access to previous chats and responses. You have access to real-time news, information and media. You will keep conversation to a minimum and answer to the best of your abilities. When you are asked to show an image, instead describe the image with [Image of <Description>]. You can SEE through the user\'s webcam only when they explicitly ask and you emit one mandatory closed [[EVA_LOOK]]{"question":"<what to look for>"}[[/EVA_LOOK]] marker; Electron asks them to authorize one frame. Signal delivery is unavailable from model output.' },
        { role: 'user', content: ((typeof getSystemPrompt === 'function') ? getSystemPrompt() : '') + " " + dateContents },
      ];

      // Store the initial messages in localStorage
      localStorage.setItem("messages", JSON.stringify(iMessages));
    }

    // --- Cognition: Fetch memory context from bridge (ephemeral — not persisted) ---
    var _gptMemoryContext = '';
    try {
      var _bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
      var _ctxResp = await fetch(_bridgeUrl.replace(/\/+$/, '') + '/v1/memory/context?message=' + encodeURIComponent(sQuestion), {
        signal: AbortSignal.timeout(3000)
      });
      if (!requestIsCurrent()) return;
      if (_ctxResp.ok) {
        var _ctxData = await _ctxResp.json();
        if (!requestIsCurrent()) return;
        if (_ctxData.context && _ctxData.cognition_enabled) {
          _gptMemoryContext = _ctxData.context;
        }
      }
    } catch (e) {}
    if (!requestIsCurrent()) return;

    // Create a new array to store the messages
    let newMessages = [];

	//const cleanedQuestion = sQuestion.replace(/<div[^>]*>|<\/div>|&nbsp;/gi, '');
	const cleanedQuestion = sQuestion.replace(/<div[^>]*>|<\/div>|&nbsp;|<span[^>]*>|<\/span>/gi, '');

    	// Push the messages to the new array
    	newMessages.push({ role: 'assistant', content: lastResponse.replace(/\n/g, ' ') });
	newMessages.push({ role: 'user', content: cleanedQuestion.replace(/\n/g, '') });

      	// External Sources
	// Check external.js for source data

	// Weather Report
        const keyword_weather = 'weather';
        if (sQuestion.includes(keyword_weather)) {
          newMessages.push({ role: 'user', content: "Today's " + weatherContents + ". " + sQuestion.replace(/\n/g, '') });
        }

        // Top Headline News
        const keyword_news = 'news';
        if (sQuestion.includes(keyword_news)) {
          newMessages.push({ role: 'user', content: "Today's " + newsContents + ". " + sQuestion.replace(/\n/g, '') });
        }

        // Markets
        const keyword_stock = 'stock';
        const keyword_markets = 'markets';
        const keyword_spy = 'SPY';
        if (sQuestion.includes(keyword_stock) || sQuestion.includes(keyword_markets) || sQuestion.includes(keyword_spy)) {
          newMessages.push({ role: 'user', content: "Today's " + marketContents + " " + sQuestion.replace(/\n/g, '') });
        }

        // Solar Space Weather
        const keyword_solar = 'solar';
        const keyword_spaceweather = 'space weather';
        if (sQuestion.includes(keyword_solar) || sQuestion.includes(keyword_spaceweather)) {
          newMessages.push({ role: 'user', content: "Today's " + solarContents + " " + sQuestion.replace(/\n/g, '') });
        }

    // Append the new messages to the existing messages in localStorage
    let existingMessages = JSON.parse(localStorage.getItem("messages")) || [];
    existingMessages = existingMessages.concat(newMessages);
    localStorage.setItem("messages", JSON.stringify(existingMessages));

    // Retrieve messages from local storage
    var cStoredMessages = localStorage.getItem("messages");
    var kMessages = cStoredMessages ? JSON.parse(cStoredMessages) : [];

        // Refresh the developer message with current capabilities (camera, etc.)
        // so existing conversations pick up new instructions without a reset.
        var _devPrompt = 'You are Eva. You have access to previous chats and responses. You have access to real-time news, information and media. You will keep conversation to a minimum and answer to the best of your abilities. When you are asked to show an image, instead describe the image with [Image of <Description>]. You can SEE through the user\'s webcam only when they explicitly ask and you emit one mandatory closed [[EVA_LOOK]]{"question":"<what to look for>"}[[/EVA_LOOK]] marker; Electron asks them to authorize one frame. Signal delivery is unavailable from model output.';
        if (kMessages.length > 0 && (kMessages[0].role === 'developer' || kMessages[0].role === 'system')) {
          kMessages[0].content = _devPrompt;
        }

        // Inject memory context into request payload only (not persisted)
        if (_gptMemoryContext && kMessages.length > 0 && (kMessages[0].role === 'developer' || kMessages[0].role === 'system')) {
          kMessages[0].content = _gptMemoryContext + '\n\n' + kMessages[0].content.replace(/^\[Morning Reflection[\s\S]*?\n\n/m, '');
        }

        // Exclude messages with the "developer" role see 
        // https://github.com/appatalks/eva-agent/issues/63#issuecomment-2492821202 
        if (isO1) {
          kMessages = kMessages.filter(msg => msg.role === 'user' || msg.role === 'assistant');
          dTemperature = 1;
        }
  // Potential gpt-5 guidance (adjust if OpenAI docs require different roles)
  // Keep roles for now; gpt-5 uses default temperature only; do not override.
	
    // API Payload
    var data = {
        model: sModel,
	messages: kMessages,
        max_completion_tokens: iMaxTokens,
        temperature:  dTemperature,
        frequency_penalty: eFrequency_penalty,
        presence_penalty: cPresence_penalty,
	stop: hStop
    }
            // Additional parameters for GPT-5 family and 'latest' alias
            if (isGpt5 || isLatest) {
              data.top_p = topP;
              // Do not send max_tokens for gpt-5; use max_completion_tokens only
              delete data.temperature; // Exclude temperature for gpt-5/latest
              delete data.stop; // Exclude stop for gpt-5/latest
            }
    if (sModel === "o3-mini") {
      data.reasoning_effort = (typeof getReasoningEffort === 'function') ? getReasoningEffort() : "medium";
      delete data.temperature; // Exclude temperature for o3-mini
    }   

    // Sending API Payload
    if (!requestIsCurrent()) return;
    oHttp.send(JSON.stringify(data));
    // console.log("gpt-core.js Line 330" + JSON.stringify(data));

    // Relay Send to Screen

  if (imgSrcGlobal) {
    var responseImage = document.createElement("img");
    responseImage.src = imgSrcGlobal;
  // no leading newline to avoid extra gaps
    // Wrap user message in bubble
    const userWrap = document.createElement('div');
    userWrap.className = 'chat-bubble user-bubble';
    const safeUserImg = (function escapeHtmlLite(str){
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    })(sQuestion.replace(/<br>/g, '\n')).replace(/\n/g, '<br>');
    userWrap.innerHTML = '<span class="user">You:</span> ' + safeUserImg;
    userWrap.appendChild(responseImage);
    txtOutput.appendChild(userWrap);
  } else {
    // Sanitize user HTML to avoid breaking the bubble but preserve line breaks
    const safeUser = (function escapeHtmlLite(str){
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    })(sQuestion.replace(/<br>/g, '\n'))
      .replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble user-bubble">' + '<span class="user">You:</span> ' + safeUser + '</div>';
    txtMsg.innerHTML = "";
    var element = document.getElementById("txtOutput");
    element.scrollTop = element.scrollHeight;
  }
  imgSrcGlobal = '';
}
