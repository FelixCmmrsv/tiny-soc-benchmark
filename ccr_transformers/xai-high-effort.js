class XaiHighEffort {
  name = "xai-high-effort";

  async transformRequestIn(request, provider) {
    request.reasoning_effort = "high";
    return request;
  }
}

module.exports = XaiHighEffort;
