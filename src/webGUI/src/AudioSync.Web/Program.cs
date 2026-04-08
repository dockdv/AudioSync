using AudioSync.Core.Merging;
using AudioSync.Core.Probing;
using AudioSync.Core.Sessions;
using AudioSync.Core.Sync;
using AudioSync.Core.Tooling;
using AudioSync.Core.Visual;
using AudioSync.Web.Endpoints;
using Microsoft.Extensions.FileProviders;

var builder = WebApplication.CreateBuilder(args);

// Default is 30s — too long for an interactive local tool. Cap shutdown drain at 2s.
builder.Services.Configure<Microsoft.Extensions.Hosting.HostOptions>(o =>
    o.ShutdownTimeout = TimeSpan.FromSeconds(2));

// Tool locator (config-driven, sidecar/PATH fallback)
var toolOpts = new ToolLocatorOptions
{
    FfmpegPath = builder.Configuration["Tools:Ffmpeg"],
    FfprobePath = builder.Configuration["Tools:Ffprobe"],
    MkvmergePath = builder.Configuration["Tools:Mkvmerge"],
};
var locator = new ToolLocator(toolOpts);
builder.Services.AddSingleton<IToolLocator>(locator);
builder.Services.AddSingleton<IProcessRunner, ProcessRunner>();
builder.Services.AddSingleton<FfLib>();

// Probing
builder.Services.AddSingleton<IMediaProber, FfprobeProber>();

// Sessions
var sessOpts = new SessionStoreOptions
{
    IdleTtl = TimeSpan.FromSeconds(builder.Configuration.GetValue<int?>("Sessions:IdleTtlSeconds") ?? 3600),
    MaxTtl = TimeSpan.FromSeconds(builder.Configuration.GetValue<int?>("Sessions:MaxTtlSeconds") ?? 7200),
    PurgeInterval = TimeSpan.FromSeconds(builder.Configuration.GetValue<int?>("Sessions:PurgeIntervalSeconds") ?? 300),
};
builder.Services.AddSingleton(sessOpts);
builder.Services.AddSingleton<SessionStore>(sp => new SessionStore(sp.GetRequiredService<SessionStoreOptions>()));
builder.Services.AddHostedService<SessionPurgeService>();

// Sync + Visual + Merging
builder.Services.AddSingleton<AudioLoader>();
builder.Services.AddSingleton<CutDetector>();
builder.Services.AddSingleton<IVisualMatcher, VisualMatcher>();
builder.Services.AddSingleton<ISyncEngine, SyncEngine>();
builder.Services.AddSingleton<MkvMerger>();
builder.Services.AddSingleton<IMerger, FfmpegMerger>();

builder.Services.ConfigureHttpJsonOptions(o =>
{
    o.SerializerOptions.PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.SnakeCaseLower;
    o.SerializerOptions.PropertyNameCaseInsensitive = true;
    o.SerializerOptions.NumberHandling =
        System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals;
});

var app = builder.Build();

app.Use(async (ctx, next) =>
{
    try { await next(); }
    catch (Microsoft.AspNetCore.Http.BadHttpRequestException ex)
    {
        ctx.Response.StatusCode = 400;
        ctx.Response.ContentType = "application/json";
        var msg = ex.InnerException?.Message ?? ex.Message;
        await ctx.Response.WriteAsync($"{{\"error\":{System.Text.Json.JsonSerializer.Serialize(msg)}}}");
    }
});

// Tool availability check (mirror of Python startup banner)
{
    var log = app.Services.GetRequiredService<ILogger<Program>>();
    log.LogInformation("ffmpeg:   {Path}", locator.Ffmpeg ?? "NOT FOUND");
    log.LogInformation("ffprobe:  {Path}", locator.Ffprobe ?? "NOT FOUND");
    log.LogInformation("mkvmerge: {Path}", locator.Mkvmerge ?? "NOT FOUND");
    log.LogInformation("hwaccel:  {Hw}", locator.Hwaccel);

    var missing = new List<string>();
    if (locator.Ffmpeg is null) missing.Add("ffmpeg  (set Tools:Ffmpeg / FFMPEG_PATH)");
    if (locator.Ffprobe is null) missing.Add("ffprobe (set Tools:Ffprobe / FFPROBE_PATH)");
    if (locator.Mkvmerge is null) missing.Add("mkvmerge (set Tools:Mkvmerge / MKVMERGE_PATH)");
    if (missing.Count > 0)
    {
        foreach (var m in missing) log.LogError("Required binary not found: {M}", m);
        Environment.Exit(1);
    }
}

// Serve wwwroot from embedded resources so the .exe is fully self-contained.
var embeddedFiles = new ManifestEmbeddedFileProvider(typeof(Program).Assembly, "wwwroot");
app.UseDefaultFiles(new DefaultFilesOptions { FileProvider = embeddedFiles });
app.UseStaticFiles(new StaticFileOptions { FileProvider = embeddedFiles });

EndpointMappings.MapAll(app);

app.Run();
