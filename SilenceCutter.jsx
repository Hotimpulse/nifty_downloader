/*
    Silence Cutter Panel PRO for Adobe After Effects
    ------------------------------------------------
    Features:
    - Detect silence using Convert Audio to Keyframes
    - Build a new comp with cuts glued together
    - Preview mode: markers only
    - Process all selected layers
    - Ripple-cut inside the same comp

    Install:
    Put this file into:
    Scripts/ScriptUI Panels/

    Restart After Effects and open:
    Window -> Silence Cutter Panel PRO
*/

(function silenceCutterPanelPRO(thisObj) {
    function buildUI(thisObj) {
        var pal = (thisObj instanceof Panel)
            ? thisObj
            : new Window("palette", "Silence Cutter Panel PRO", undefined, { resizeable: true });

        if (!pal) return pal;

        pal.orientation = "column";
        pal.alignChildren = ["fill", "top"];
        pal.spacing = 8;
        pal.margins = 12;
        var lastErrorDetails = "";

        function addLabeledField(parent, labelText, defaultValue, helpText) {
            var group = parent.add("group");
            group.orientation = "row";
            group.alignChildren = ["left", "center"];
            group.spacing = 8;

            var label = group.add("statictext", undefined, labelText);
            label.preferredSize.width = 185;

            var field = group.add("edittext", undefined, defaultValue);
            field.characters = 10;
            if (helpText) field.helpTip = helpText;

            return field;
        }

        var settingsPanel = pal.add("panel", undefined, "Detection Settings");
        settingsPanel.orientation = "column";
        settingsPanel.alignChildren = ["fill", "top"];
        settingsPanel.spacing = 6;
        settingsPanel.margins = 10;

        var thresholdField = addLabeledField(
            settingsPanel,
            "Silence threshold:",
            "5",
            "Sound is detected when Both Channels slider is >= this value."
        );

        var minSoundField = addLabeledField(
            settingsPanel,
            "Min sound length (frames):",
            "4",
            "Sound bursts shorter than this are ignored."
        );

        var minSilenceField = addLabeledField(
            settingsPanel,
            "Min silence length to cut (frames):",
            "6",
            "Silent gaps shorter than this are preserved."
        );

        var paddingField = addLabeledField(
            settingsPanel,
            "Padding before/after (frames):",
            "2",
            "Extra frames kept around each sound region."
        );

        var scopePanel = pal.add("panel", undefined, "Processing Scope");
        scopePanel.orientation = "column";
        scopePanel.alignChildren = ["left", "top"];
        scopePanel.spacing = 4;
        scopePanel.margins = 10;

        var processAllCheckbox = scopePanel.add("checkbox", undefined, "Process all selected layers");
        processAllCheckbox.value = false;

        var useWorkAreaCheckbox = scopePanel.add("checkbox", undefined, "Restrict analysis to work area");
        useWorkAreaCheckbox.value = false;

        var optionsPanel = pal.add("panel", undefined, "Output Mode");
        optionsPanel.orientation = "column";
        optionsPanel.alignChildren = ["left", "top"];
        optionsPanel.spacing = 4;
        optionsPanel.margins = 10;

        var previewOnlyCheckbox = optionsPanel.add("checkbox", undefined, "Preview mode (markers only)");
        previewOnlyCheckbox.value = false;

        var rippleSameCompCheckbox = optionsPanel.add("checkbox", undefined, "Ripple-cut inside current comp");
        rippleSameCompCheckbox.value = false;

        var markersCheckbox = optionsPanel.add("checkbox", undefined, "Add markers for kept segments");
        markersCheckbox.value = true;

        var keepTempCheckbox = optionsPanel.add("checkbox", undefined, "Keep temporary analysis comps");
        keepTempCheckbox.value = false;

        var originalPanel = pal.add("panel", undefined, "Original Layer Handling");
        originalPanel.orientation = "column";
        originalPanel.alignChildren = ["left", "top"];
        originalPanel.spacing = 4;
        originalPanel.margins = 10;

        var disableOriginalCheckbox = originalPanel.add("checkbox", undefined, "Disable original layer after ripple rebuild");
        disableOriginalCheckbox.value = true;

        var shyGeneratedCheckbox = originalPanel.add("checkbox", undefined, "Shy generated layers");
        shyGeneratedCheckbox.value = false;

        var buttonGroup = pal.add("group");
        buttonGroup.orientation = "row";
        buttonGroup.alignChildren = ["fill", "center"];
        buttonGroup.spacing = 8;

        var previewBtn = buttonGroup.add("button", undefined, "Preview");
        var runBtn = buttonGroup.add("button", undefined, "Process");
        var copyErrorBtn = buttonGroup.add("button", undefined, "Copy Last Error");
        copyErrorBtn.enabled = false;
        var helpBtn = buttonGroup.add("button", undefined, "Help");

        var statusPanel = pal.add("panel", undefined, "Status");
        statusPanel.orientation = "column";
        statusPanel.alignChildren = ["fill", "top"];
        statusPanel.margins = 10;

        var statusText = statusPanel.add("statictext", undefined, "Ready.", { multiline: true });
        statusText.minimumSize.height = 80;

        function setStatus(msg) {
            statusText.text = msg;
            pal.layout.layout(true);
        }

        function parseNumber(field, fallback, isInt) {
            var val = isInt ? parseInt(field.text, 10) : parseFloat(field.text);
            return isNaN(val) ? fallback : val;
        }

        function activateCompViewer(comp) {
            if (!comp) return null;

            var viewer = comp.openInViewer();
            if (viewer && viewer.setActive) {
                viewer.setActive();
            }
            return viewer;
        }

        function removeAllSelections(comp) {
            for (var i = 1; i <= comp.numLayers; i++) {
                comp.layer(i).selected = false;
            }
        }

        function getTopLayer(comp) {
            if (!comp || comp.numLayers < 1) return null;
            return comp.layer(1);
        }

        function copyLayerToCompOrThrow(srcLayer, destComp, contextLabel) {
            var beforeCount = destComp.numLayers;

            srcLayer.copyToComp(destComp);

            if (destComp.numLayers <= beforeCount) {
                throw new Error("Failed to duplicate layer into " + contextLabel + ": " + srcLayer.name);
            }

            var copiedLayer = getTopLayer(destComp);
            if (!copiedLayer) {
                throw new Error("Copied layer could not be resolved in " + contextLabel + ": " + srcLayer.name);
            }

            return copiedLayer;
        }

        function ensureAudioLayer(srcLayer) {
            if (!srcLayer) {
                throw new Error("A selected layer reference became invalid.");
            }

            if (!(srcLayer instanceof AVLayer)) {
                throw new Error("Layer '" + srcLayer.name + "' is not an AV layer and cannot be analyzed.");
            }

            if (!srcLayer.hasAudio) {
                throw new Error("Layer '" + srcLayer.name + "' does not contain audio to analyze.");
            }
        }

        function cleanupTempComp(tempComp, sourceComp, keepTemp) {
            if (!tempComp || keepTemp) return;

            activateCompViewer(sourceComp);
            tempComp.remove();
        }

        function buildErrorDetails(err, stage, layerName, settings) {
            var lines = [];
            lines.push("Silence Cutter Panel PRO");
            lines.push("Stage: " + stage);

            if (layerName) {
                lines.push("Layer: " + layerName);
            }

            lines.push("Message: " + err.toString());

            if (err.line) {
                lines.push("Line: " + err.line);
            }

            if (err.fileName) {
                lines.push("File: " + err.fileName);
            }

            if (settings) {
                lines.push(
                    "Settings: threshold=" + settings.threshold +
                    ", minSoundFrames=" + settings.minSoundFrames +
                    ", minSilenceFrames=" + settings.minSilenceFrames +
                    ", paddingFrames=" + settings.paddingFrames +
                    ", processAll=" + settings.processAll +
                    ", useWorkArea=" + settings.useWorkArea +
                    ", previewOnly=" + settings.previewOnly +
                    ", rippleSameComp=" + settings.rippleSameComp +
                    ", addMarkers=" + settings.addMarkers +
                    ", keepTemp=" + settings.keepTemp
                );
            }

            return lines.join("\r\n");
        }

        function copyTextToClipboard(text) {
            if (!text) return false;

            if ($.os.toLowerCase().indexOf("windows") === -1) {
                return false;
            }

            var tempFile = new File(Folder.temp.fsName + "/silence_cutter_last_error.txt");
            tempFile.encoding = "UTF-8";

            if (!tempFile.open("w")) {
                return false;
            }

            tempFile.write(text);
            tempFile.close();

            var escapedPath = tempFile.fsName.replace(/\\/g, "\\\\").replace(/'/g, "''");
            system.callSystem(
                'powershell -NoProfile -ExecutionPolicy Bypass -Command "Set-Clipboard -Value (Get-Content -LiteralPath \'' +
                escapedPath +
                '\' -Raw)"'
            );

            return true;
        }

        function findAudioAmplitudeLayer(comp) {
            for (var i = 1; i <= comp.numLayers; i++) {
                if (comp.layer(i).name === "Audio Amplitude") {
                    return comp.layer(i);
                }
            }
            return null;
        }

        function findBothChannelsSlider(audioAmpLayer) {
            var effects = audioAmpLayer.property("ADBE Effect Parade");
            if (!effects) return null;

            for (var i = 1; i <= effects.numProperties; i++) {
                var fx = effects.property(i);
                if (fx.name === "Both Channels") {
                    return fx.property(1);
                }
            }
            return null;
        }

        function mergeSegments(segments, gapThreshold) {
            if (segments.length === 0) return [];

            var merged = [[segments[0][0], segments[0][1]]];
            for (var i = 1; i < segments.length; i++) {
                var prev = merged[merged.length - 1];
                var cur = segments[i];
                var gap = cur[0] - prev[1];

                if (gap < gapThreshold) {
                    prev[1] = Math.max(prev[1], cur[1]);
                } else {
                    merged.push([cur[0], cur[1]]);
                }
            }
            return merged;
        }

        function collectTargetLayers(comp, processAll) {
            if (comp.selectedLayers.length < 1) {
                throw new Error("Select at least one layer with audio.");
            }

            if (processAll) {
                var arr = [];
                for (var i = 0; i < comp.selectedLayers.length; i++) {
                    arr.push(comp.selectedLayers[i]);
                }
                return arr;
            }

            return [comp.selectedLayers[0]];
        }

        function getScanRange(comp, layer, useWorkArea) {
            var scanStart, scanEnd;

            if (useWorkArea) {
                scanStart = comp.workAreaStart;
                scanEnd = comp.workAreaStart + comp.workAreaDuration;
            } else {
                scanStart = layer.inPoint;
                scanEnd = layer.outPoint;
            }

            scanStart = Math.max(0, scanStart);
            scanEnd = Math.min(comp.duration, scanEnd);

            if (scanEnd <= scanStart) {
                throw new Error("Invalid analysis range for layer: " + layer.name);
            }

            return {
                start: scanStart,
                end: scanEnd
            };
        }

        function analyzeLayer(comp, srcLayer, settings, statusPrefix) {
            var frameDur = comp.frameDuration;
            var minSound = settings.minSoundFrames * frameDur;
            var minSilence = settings.minSilenceFrames * frameDur;
            var padding = settings.paddingFrames * frameDur;
            var tempComp = null;

            ensureAudioLayer(srcLayer);

            var scanRange = getScanRange(comp, srcLayer, settings.useWorkArea);
            var scanStart = scanRange.start;
            var scanEnd = scanRange.end;

            try {
                setStatus(statusPrefix + "Creating temp analysis comp...");

                tempComp = app.project.items.addComp(
                    comp.name + "_AUDIO_ANALYSIS_TMP_" + srcLayer.index,
                    comp.width,
                    comp.height,
                    comp.pixelAspect,
                    comp.duration,
                    comp.frameRate
                );

                var analysisLayer = copyLayerToCompOrThrow(srcLayer, tempComp, "temp analysis comp");

                activateCompViewer(tempComp);
                removeAllSelections(tempComp);
                analysisLayer.selected = true;

                setStatus(statusPrefix + "Converting audio to keyframes...");

                var cmdId = app.findMenuCommandId("Convert Audio to Keyframes");
                if (!cmdId) {
                    throw new Error("Could not find 'Convert Audio to Keyframes'.");
                }

                app.executeCommand(cmdId);

                var audioAmpLayer = findAudioAmplitudeLayer(tempComp);
                if (!audioAmpLayer) {
                    throw new Error("Audio Amplitude layer was not created for layer: " + srcLayer.name);
                }

                var bothChannels = findBothChannelsSlider(audioAmpLayer);
                if (!bothChannels) {
                    throw new Error("Could not find 'Both Channels' slider for layer: " + srcLayer.name);
                }

                setStatus(statusPrefix + "Scanning amplitude...");

                var rawSegments = [];
                var sounding = false;
                var soundStart = scanStart;

                var t = scanStart;
                while (t <= scanEnd + frameDur * 0.5) {
                    var sampleTime = Math.min(t, scanEnd);
                    var value = bothChannels.valueAtTime(sampleTime, false);

                    if (!sounding && value >= settings.threshold) {
                        sounding = true;
                        soundStart = sampleTime;
                    } else if (sounding && value < settings.threshold) {
                        sounding = false;
                        rawSegments.push([soundStart, sampleTime]);
                    }

                    t += frameDur;
                }

                if (sounding) {
                    rawSegments.push([soundStart, scanEnd]);
                }

                if (rawSegments.length === 0) {
                    cleanupTempComp(tempComp, comp, settings.keepTemp);
                    if (settings.keepTemp) {
                        activateCompViewer(comp);
                    }
                    return {
                        segments: [],
                        tempComp: settings.keepTemp ? tempComp : null,
                        scanStart: scanStart,
                        scanEnd: scanEnd
                    };
                }

                setStatus(statusPrefix + "Filtering short bursts...");

                var filtered = [];
                for (var i = 0; i < rawSegments.length; i++) {
                    var seg = rawSegments[i];
                    if ((seg[1] - seg[0]) >= minSound) {
                        filtered.push(seg);
                    }
                }

                if (filtered.length === 0) {
                    cleanupTempComp(tempComp, comp, settings.keepTemp);
                    if (settings.keepTemp) {
                        activateCompViewer(comp);
                    }
                    return {
                        segments: [],
                        tempComp: settings.keepTemp ? tempComp : null,
                        scanStart: scanStart,
                        scanEnd: scanEnd
                    };
                }

                setStatus(statusPrefix + "Merging nearby regions...");

                var merged = mergeSegments(filtered, minSilence);

                var padded = [];
                for (var j = 0; j < merged.length; j++) {
                    var s = Math.max(scanStart, merged[j][0] - padding);
                    var e = Math.min(scanEnd, merged[j][1] + padding);

                    if (padded.length > 0 && s <= padded[padded.length - 1][1]) {
                        padded[padded.length - 1][1] = Math.max(padded[padded.length - 1][1], e);
                    } else {
                        padded.push([s, e]);
                    }
                }

                if (!settings.keepTemp) {
                    cleanupTempComp(tempComp, comp, false);
                    tempComp = null;
                } else {
                    activateCompViewer(comp);
                }

                return {
                    segments: padded,
                    tempComp: tempComp,
                    scanStart: scanStart,
                    scanEnd: scanEnd
                };
            } catch (err) {
                if (tempComp) {
                    cleanupTempComp(tempComp, comp, settings.keepTemp);
                    if (settings.keepTemp) {
                        activateCompViewer(comp);
                    }
                }
                throw err;
            }
        }

        function addPreviewMarkers(comp, srcLayer, segments, clearOld) {
            if (!comp.markerProperty) return;

            if (clearOld) {
                while (comp.markerProperty.numKeys > 0) {
                    comp.markerProperty.removeKey(1);
                }
            }

            for (var i = 0; i < segments.length; i++) {
                var segStart = segments[i][0];
                var segEnd = segments[i][1];

                var marker = new MarkerValue(
                    "[Preview] " + srcLayer.name +
                    " | segment " + (i + 1) +
                    " | " + segStart.toFixed(2) + "s -> " + segEnd.toFixed(2) + "s"
                );
                marker.duration = Math.max(0, segEnd - segStart);
                comp.markerProperty.setValueAtTime(segStart, marker);
            }
        }

        function buildOutputComp(comp, srcLayer, segments, addMarkers) {
            var totalDuration = 0;
            for (var i = 0; i < segments.length; i++) {
                totalDuration += (segments[i][1] - segments[i][0]);
            }
            totalDuration = Math.max(totalDuration, comp.frameDuration);

            var outComp = app.project.items.addComp(
                comp.name + "_" + sanitizeName(srcLayer.name) + "_NO_SILENCE",
                comp.width,
                comp.height,
                comp.pixelAspect,
                totalDuration,
                comp.frameRate
            );

            var cursor = 0;
            for (var j = 0; j < segments.length; j++) {
                var segStart = segments[j][0];
                var segEnd = segments[j][1];
                var segDur = segEnd - segStart;

                var newLayer = copyLayerToCompOrThrow(srcLayer, outComp, "output comp");

                newLayer.startTime = cursor - (segStart - srcLayer.startTime);
                newLayer.inPoint = cursor;
                newLayer.outPoint = cursor + segDur;

                if (addMarkers) {
                    var marker = new MarkerValue(
                        srcLayer.name +
                        " | segment " + (j + 1) +
                        " | src " + segStart.toFixed(2) +
                        "s -> " + segEnd.toFixed(2) + "s"
                    );
                    outComp.markerProperty.setValueAtTime(cursor, marker);
                }

                cursor += segDur;
            }

            return outComp;
        }

        function rippleCutInSameComp(comp, srcLayer, segments, settings) {
            var generated = [];
            var cursor = srcLayer.inPoint;
            var labelColor = srcLayer.label;

            for (var i = 0; i < segments.length; i++) {
                var segStart = segments[i][0];
                var segEnd = segments[i][1];
                var segDur = segEnd - segStart;

                var newLayer = copyLayerToCompOrThrow(srcLayer, comp, "current comp");

                newLayer.name = srcLayer.name + "_SC_" + (i + 1);
                newLayer.label = labelColor;
                newLayer.startTime = cursor - (segStart - srcLayer.startTime);
                newLayer.inPoint = cursor;
                newLayer.outPoint = cursor + segDur;
                newLayer.moveBefore(srcLayer);

                if (settings.shyGenerated) {
                    newLayer.shy = true;
                }

                if (settings.addMarkers) {
                    var marker = new MarkerValue(
                        srcLayer.name +
                        " | segment " + (i + 1) +
                        " | src " + segStart.toFixed(2) +
                        "s -> " + segEnd.toFixed(2) + "s"
                    );
                    comp.markerProperty.setValueAtTime(cursor, marker);
                }

                generated.push(newLayer);
                cursor += segDur;
            }

            if (settings.disableOriginal) {
                srcLayer.enabled = false;
                srcLayer.audioEnabled = false;
            }

            return generated;
        }

        function sanitizeName(name) {
            return name.replace(/[\\\/\:\*\?\"\<\>\|]/g, "_");
        }

        function getSettings(previewOverride) {
            var s = {
                threshold: parseNumber(thresholdField, 5, false),
                minSoundFrames: parseNumber(minSoundField, 4, true),
                minSilenceFrames: parseNumber(minSilenceField, 6, true),
                paddingFrames: parseNumber(paddingField, 2, true),
                processAll: processAllCheckbox.value,
                useWorkArea: useWorkAreaCheckbox.value,
                previewOnly: previewOverride === true ? true : previewOnlyCheckbox.value,
                rippleSameComp: rippleSameCompCheckbox.value,
                addMarkers: markersCheckbox.value,
                keepTemp: keepTempCheckbox.value,
                disableOriginal: disableOriginalCheckbox.value,
                shyGenerated: shyGeneratedCheckbox.value
            };

            if (s.minSoundFrames < 1) s.minSoundFrames = 1;
            if (s.minSilenceFrames < 1) s.minSilenceFrames = 1;
            if (s.paddingFrames < 0) s.paddingFrames = 0;

            return s;
        }

        function process(previewOverride) {
            var comp = null;
            var settings = null;
            var currentLayerName = "";
            app.beginUndoGroup("Silence Cutter Panel PRO");

            try {
                comp = app.project.activeItem;
                if (!(comp && comp instanceof CompItem)) {
                    throw new Error("Open a composition first.");
                }

                settings = getSettings(previewOverride);
                var targetLayers = collectTargetLayers(comp, settings.processAll);

                if (settings.previewOnly && settings.addMarkers && comp.markerProperty) {
                    while (comp.markerProperty.numKeys > 0) {
                        comp.markerProperty.removeKey(1);
                    }
                }

                var report = [];
                var createdComps = [];
                var rebuiltLayersTotal = 0;

                for (var i = 0; i < targetLayers.length; i++) {
                    var srcLayer = targetLayers[i];
                    currentLayerName = srcLayer ? srcLayer.name : "";
                    var prefix = "[" + (i + 1) + "/" + targetLayers.length + "] " + srcLayer.name + ": ";

                    setStatus(prefix + "Starting...");

                    var analysis = analyzeLayer(comp, srcLayer, settings, prefix);
                    var segments = analysis.segments;

                    if (segments.length === 0) {
                        report.push(srcLayer.name + ": no non-silent regions found");
                        continue;
                    }

                    if (settings.previewOnly) {
                        if (settings.addMarkers) {
                            addPreviewMarkers(comp, srcLayer, segments, false);
                        }
                        report.push(srcLayer.name + ": previewed " + segments.length + " segment(s)");
                        continue;
                    }

                    if (settings.rippleSameComp) {
                        var generated = rippleCutInSameComp(comp, srcLayer, segments, settings);
                        rebuiltLayersTotal += generated.length;
                        report.push(srcLayer.name + ": rebuilt " + generated.length + " segment(s) in current comp");
                    } else {
                        var outComp = buildOutputComp(comp, srcLayer, segments, settings.addMarkers);
                        createdComps.push(outComp);
                        report.push(srcLayer.name + ": created comp '" + outComp.name + "' with " + segments.length + " segment(s)");
                    }
                }

                lastErrorDetails = "";
                copyErrorBtn.enabled = false;

                if (!settings.previewOnly && createdComps.length > 0) {
                    activateCompViewer(createdComps[0]);
                } else {
                    activateCompViewer(comp);
                }

                var summary = "Done.\n\n";
                for (var r = 0; r < report.length; r++) {
                    summary += "- " + report[r] + "\n";
                }

                if (!settings.previewOnly && settings.rippleSameComp) {
                    summary += "\nGenerated layers: " + rebuiltLayersTotal;
                }

                if (!settings.previewOnly && !settings.rippleSameComp) {
                    summary += "\nCreated comps: " + createdComps.length;
                }

                setStatus(summary);
            } catch (err) {
                if (comp && comp instanceof CompItem) {
                    activateCompViewer(comp);
                }
                lastErrorDetails = buildErrorDetails(err, "process", currentLayerName, settings);
                copyErrorBtn.enabled = true;
                setStatus("Error: " + err.toString() + "\n\nUse 'Copy Last Error' to copy the full debug report.");
                alert("Silence Cutter Panel PRO\n\n" + err.toString() + "\n\nUse 'Copy Last Error' to copy the full debug report.");
            } finally {
                app.endUndoGroup();
            }
        }

        previewBtn.onClick = function () {
            process(true);
        };

        runBtn.onClick = function () {
            process(false);
        };

        copyErrorBtn.onClick = function () {
            if (!lastErrorDetails) {
                alert("Silence Cutter Panel PRO\n\nNo error has been captured yet.");
                return;
            }

            if (copyTextToClipboard(lastErrorDetails)) {
                setStatus("Last error copied to clipboard.");
            } else {
                alert("Silence Cutter Panel PRO\n\nCould not copy the error to the clipboard on this system.");
            }
        };

        helpBtn.onClick = function () {
            alert(
                "Silence Cutter Panel PRO\n\n" +
                "Modes:\n" +
                "1. Preview: adds markers only.\n" +
                "2. Output comp: creates a new comp with glued segments.\n" +
                "3. Ripple in current comp: duplicates kept regions back-to-back in the same comp.\n\n" +
                "Recommended starting values:\n" +
                "- Threshold: 5\n" +
                "- Min sound: 4 frames\n" +
                "- Min silence: 6 frames\n" +
                "- Padding: 2 frames\n\n" +
                "Tips:\n" +
                "- Lower threshold if quiet speech is missed.\n" +
                "- Raise threshold if noise gets treated as speech.\n" +
                "- Increase padding for more natural pacing.\n" +
                "- For ripple mode, use a dedicated source layer when possible.\n" +
                "- Process all selected layers will analyze each selected layer independently."
            );
        };

        pal.onResizing = pal.onResize = function () {
            this.layout.resize();
        };

        pal.layout.layout(true);
        return pal;
    }

    var myPal = buildUI(thisObj);

    if (myPal instanceof Window) {
        myPal.center();
        myPal.show();
    } else {
        myPal.layout.layout(true);
        myPal.layout.resize();
    }
})(this);
