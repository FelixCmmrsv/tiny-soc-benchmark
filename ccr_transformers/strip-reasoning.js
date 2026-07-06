// Generic, safe default for OpenAI-compatible endpoints (DeepSeek, Together,
// Fireworks, OpenRouter's OpenAI format, and most local servers -- vLLM,
// Ollama, LM Studio, TGI, llama.cpp).
//
// Claude Code always sends its own extended-thinking request shape
// (`reasoning` / `thinking` / `enable_thinking`) regardless of backend.
// Strict OpenAI-compatible APIs reject unrecognized top-level fields with a
// 400; lenient ones ignore them. Stripping these fields is harmless in both
// cases, so it's a good universal default.
//
// Unlike openai-reasoning-fix.js, this does NOT rename max_tokens ->
// max_completion_tokens: that rename is specific to OpenAI's own reasoning
// models (gpt-5.x / o-series) and would BREAK providers that expect the
// standard `max_tokens` (which is nearly everyone else). Use
// openai-reasoning-fix for OpenAI reasoning models; use this for everything
// else OpenAI-compatible.
class StripReasoning {
  name = "strip-reasoning";

  async transformRequestIn(request, provider) {
    delete request.reasoning;
    delete request.thinking;
    delete request.enable_thinking;
    return request;
  }
}

module.exports = StripReasoning;
