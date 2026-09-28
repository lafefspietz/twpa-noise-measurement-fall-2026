
averages = {};

load_file('averaged_traces.json').then(
    raw_data => {
        averages = JSON.parse(raw_data);
    }
);

function setup() {

    let container = document.getElementById('p5-canvas-container');
    let squareSize = Math.min(windowWidth, windowHeight) - 50;
    let canvas = createCanvas(squareSize, squareSize);
    canvas.parent('p5-canvas-container');
    frameRate(20);
    
}

traceIndex = 0;
delta = 1;

function draw(){
    background(159,135,103);
    fill(0);
    
    stroke(0);
    strokeWeight(3);
    for(let index = 0;index < averages.fghz.length;index++){
        point(map(index,0,averages.fghz.length,0,width),map(averages.traces[traceIndex][index],0,100000,height/2,0));
    }

    strokeWeight(5);
    stroke(0);
    noFill();
    beginShape();
    for(let index = 0;index < averages.fghz.length;index++){
        vertex(map(index,0,averages.fghz.length,0,width),map(averages.fit_function[index],0,100000,height/2,0));
    }
    endShape();
    
    stroke(0);
    for(let index = 0;index < averages.fghz.length;index++){
        point(map(index,0,averages.fghz.length,0,width), map(Math.sqrt(averages.n[traceIndex])*(averages.traces[traceIndex][index] - averages.fit_function[index]),-100000,100000,height,height/2) );
    }    
    traceIndex += delta;
    if(traceIndex == 100){
        delta = -delta;
    }
    if(traceIndex < 0){
        delta = -delta;
        traceIndex=0;
    }
}

function load_file(name) {
    return fetch('load-file.php?filename=' + name).then(res => res.text());
}


