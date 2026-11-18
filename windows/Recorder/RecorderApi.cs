using System.Net.Http.Json;
using System.Text.Json.Nodes;

namespace Recorder;

internal sealed class RecorderApi : IDisposable
{
    private readonly HttpClient client = new(new HttpClientHandler { AllowAutoRedirect = false, UseProxy = false });

    public RecorderApi(string? address)
    {
        if (!Uri.TryCreate(address, UriKind.Absolute, out var uri) ||
            uri.Scheme != "http" || uri.Host != "127.0.0.1" || !uri.Query.StartsWith("?t="))
            throw new InvalidOperationException("Open AgentDuet Recorder from the Start menu.");
        client.BaseAddress = new Uri(uri, "/api/");
        client.DefaultRequestHeaders.Add("X-Token", uri.Query[3..]);
    }

    public async Task<JsonNode> Request(string path, object? body = null)
    {
        using var response = body is null ? await client.GetAsync(path) : await client.PostAsJsonAsync(path, body);
        response.EnsureSuccessStatusCode();
        var data = await response.Content.ReadFromJsonAsync<JsonNode>() ?? throw new IOException("Empty Recorder response.");
        if ((bool?)data["ok"] == false)
            throw new InvalidOperationException((string?)data["message"] ?? "Recorder could not save this change.");
        return data;
    }

    public void Dispose() => client.Dispose();
}
