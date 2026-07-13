// OpenAI image generation (gpt-image-1)

function dalle3Send() {
            // Get user input from the form
            const prompt = document.getElementById("txtMsg").innerHTML
                .replace(/<br>/g, '\n')
                .replace(/<div[^>]*>|<\/div>|&nbsp;|<span[^>]*>|<\/span>/gi, '')
                .trim();

            if (!prompt) {
                alert("Type in your prompt!");
                document.getElementById("txtMsg").focus();
                return;
            }

            // Check if the API key is available
            var apiKey = (typeof getAuthKey === 'function') ? getAuthKey('OPENAI_API_KEY') : (typeof OPENAI_API_KEY !== 'undefined' ? OPENAI_API_KEY : '');
            if (!apiKey) {
                alert("OpenAI API key not available. Please check your configuration.");
                return;
            }

            // Clear input and display user message (escaped)
            document.getElementById("txtMsg").innerHTML = "";
            var safePrompt = (typeof escapeHtml === 'function') ? escapeHtml(prompt) : prompt.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            var txtOutput = document.getElementById("txtOutput");
            txtOutput.innerHTML += '<div class="chat-bubble user-bubble"><span class="user">You:</span> ' + safePrompt + '</div>';
            txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> Here is a generated image of that description...</div>';

            // Send an API request using JavaScript fetch
            fetch("https://api.openai.com/v1/images/generations", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + apiKey
                },
                body: JSON.stringify({
                    "model": "gpt-image-1",
                    "prompt": prompt,
                    "n": 1, // Request n images
                    "size": "1024x1024" // 1024x1024, 1536x1024, 1024x1536, or auto for gpt-image-1
                })
            })
            .then(response => response.json())
            .then(data => {
                // Display each generated image in the result div
                data.data.forEach((image, index) => {
                    const imgElement = document.createElement("img");
                    // gpt-image-1 returns base64; legacy models return a hosted url.
                    const candidate = image.b64_json ? ("data:image/png;base64," + image.b64_json) : image.url;
                    const src = (typeof _safeInlineImageUrl === 'function')
                        ? _safeInlineImageUrl(candidate) : '';
                    if (!src) return;
                    imgElement.src = src;
                    imgElement.alt = `Generated Image ${index + 1}`;

                    // Create an anchor element and set attributes for opening in a new tab
                    const linkElement = document.createElement("a");
                    linkElement.href = src; // Set the image source as the link's destination
                    linkElement.target = "_blank"; // Ensures the link opens in a new tab
                    linkElement.rel = "noopener noreferrer";
                    linkElement.appendChild(imgElement); // Append the image to the anchor element

                    // Append the anchor element (which contains the image) to the result div
                    document.getElementById("txtOutput").appendChild(linkElement);
                    var element = document.getElementById("txtOutput");
                    element.scrollTop = element.scrollHeight;
                });
            })
            .catch(error => {
                console.error("Error:", error);
            });
        
}

