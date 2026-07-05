class OpenAIReasoningFix {
  name = "openai-reasoning-fix";

  async transformRequestIn(request, provider) {
    // Claude Code sends its own extended-thinking request shape regardless
    // of backend. OpenAI's actual Chat Completions API rejects any
    // unrecognized top-level field outright (400 "Unknown parameter"),
    // unlike more lenient providers -- so these must be removed, not just
    // supplemented like xai-high-effort.js does.
    delete request.reasoning;
    delete request.thinking;
    delete request.enable_thinking;

    // Reasoning-model family also renamed max_tokens -> max_completion_tokens
    // (400 "Unsupported parameter: 'max_tokens'"). ccr's built-in "maxtoken"
    // transformer only clamps an existing max_tokens value, it doesn't
    // rename it -- do the rename here instead.
    if (request.max_tokens !== undefined && request.max_completion_tokens === undefined) {
      request.max_completion_tokens = request.max_tokens;
      delete request.max_tokens;
    }

    return request;
  }
}

module.exports = OpenAIReasoningFix;
