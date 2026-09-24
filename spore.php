<?php
    $sporeUrl = "https://raw.githubusercontent.com/lafefspietz/twpa-noise-measurement-fall-2026/refs/heads/main/spore.json";
    $baseUrl = explode("spore.json",$sporeUrl)[0];
    $spore = json_decode(file_get_contents($sporeUrl), true);
    $files = $spore['files'];    
    foreach ($files as $file) {
        @copy($baseUrl.$file,$file);
    }
?>
<a href = "index.html">index.html</a>
<style>
a{n
    font-size:3em;
    color:blue;
    font-family:arial;
}
</style>