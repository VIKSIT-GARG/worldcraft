import React, { useRef, useMemo, useEffect, Component } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { OrbitControls, Line } from '@react-three/drei';
import * as THREE from 'three';

function Box({position,args,color,...props}){return <mesh castShadow receiveShadow position={position} {...props}><boxGeometry args={args}/><meshStandardMaterial color={color} roughness={.75}/></mesh>}
function Label({position,text,hospital=false}){
 const label=useMemo(()=>{
  const canvas=document.createElement('canvas'),context=canvas.getContext('2d');
  context.font='600 30px Arial';const width=Math.ceil(context.measureText(text).width)+36;
  canvas.width=width;canvas.height=56;context.font='600 30px Arial';
  context.fillStyle='#f7f8ee';context.fillRect(0,0,width,56);
  context.strokeStyle='#c6d2bc';context.strokeRect(1,1,width-2,54);
  context.fillStyle=hospital?'#ba624d':'#355c40';context.textBaseline='middle';context.fillText(text,18,28);
  return {texture:new THREE.CanvasTexture(canvas),width:width*.17};
 },[text,hospital]);
 useEffect(()=>()=>label.texture.dispose(),[label]);
 return <sprite position={position} scale={[label.width,9.5,1]} renderOrder={20}><spriteMaterial map={label.texture} transparent depthTest={false} depthWrite={false}/></sprite>
}
function Building({b}){
  const hospital=b.kind==='hospital', w=hospital?48:35, h=b.height;
  const windows=useMemo(()=>Array.from({length:Math.floor(h/10)},(_,r)=>Array.from({length:3},(_,c)=>[b.x-10+c*10,7+r*10,b.z+18])).flat(),[b]);
  return <group>
    <Box position={[b.x,1.5,b.z]} args={[w+9,3,47]} color="#e1ddcb"/>
    <Box position={[b.x,h/2+3,b.z]} args={[w,h,36]} color={hospital?'#e6e8d9':'#b5c8b4'}/>
    <Box position={[b.x,h+4,b.z]} args={[w+3,3,39]} color={hospital?'#4d7765':'#6d8b77'}/>
    <Box position={[b.x+7,h+8,b.z-5]} args={[12,6,13]} color="#8a9c8e"/>
    {windows.map((p,i)=><Box key={i} position={p} args={[5,5,1]} color={hospital?'#6d9f96':'#70938c'}/>)}
    {hospital&&<><Box position={[b.x,h+7,b.z+20]} args={[12,4,1]} color="#e77764"/><Box position={[b.x,h+7,b.z+20.5]} args={[4,12,1]} color="#e77764"/></>}
    <Label position={[b.x,h+24,b.z]} hospital={hospital} text={hospital?'✚ HOSPITAL':b.label.toUpperCase()}/>
  </group>
}
function Tree({x,z}){return <group position={[x,0,z]}><Box position={[0,6,0]} args={[2,12,2]} color="#867e60"/><mesh castShadow position={[0,15,0]}><icosahedronGeometry args={[9,1]}/><meshStandardMaterial color="#6f9164" roughness={1}/></mesh></group>}
function Road({r,world,blocked}){
  const a=world.nodes[r.a],b=world.nodes[r.b],hor=a[1]===b[1],main=r.kind==='main';
  return <group>
    <Box position={[(a[0]+b[0])/2,.4,(a[1]+b[1])/2]} args={hor?[r.length+18,.8,main?23:18]:[18,.8,r.length+18]} color={blocked?'#9c7264':main?'#677870':'#8a9689'}/>
    {[.22,.5,.78].map(t=><Box key={t} position={[a[0]+(b[0]-a[0])*t,1.05,a[1]+(b[1]-a[1])*t]} args={hor?[12,.1,1]:[1,.1,12]} color={blocked?'#efb59a':'#d9dccc'}/>)}
    {blocked&&<Box position={[(a[0]+b[0])/2,3,(a[1]+b[1])/2]} args={hor?[3,5,22]:[18,5,3]} color="#e58766"/>}
  </group>
}
function Ambulance({a,index}){
  const ref=useRef();
  useFrame((_,dt)=>{
    if(!ref.current)return;
    const target=new THREE.Vector3(a.position[0],0,a.position[1]+(a.mission?0:index*12));
    const delta=target.clone().sub(ref.current.position);
    if(delta.length()>.2){ref.current.rotation.y=Math.atan2(delta.x,delta.z);ref.current.position.lerp(target,1-Math.exp(-dt*14))}
  });
  return <group ref={ref} position={[a.position[0],0,a.position[1]+index*12]}>
    <Box position={[0,5,0]} args={[10,8,18]} color="#f7f5e9"/>
    <Box position={[0,7,5]} args={[9,5,6]} color="#476c65"/>
    <Box position={[0,10,-2]} args={[7,2,3]} color="#e87965"/>
    <Box position={[5.1,5,-3]} args={[.3,2,6]} color="#df775f"/><Box position={[5.2,5,-3]} args={[.3,5,2]} color="#df775f"/>
    {[-5,5].flatMap(x=>[-5,5].map(z=><mesh key={`${x},${z}`} position={[x,2,z]} rotation={[0,0,Math.PI/2]}><cylinderGeometry args={[2,2,1.4,10]}/><meshStandardMaterial color="#2c3b33"/></mesh>))}
  </group>
}
function AutoFit(){const {camera,size}=useThree();useEffect(()=>{camera.zoom=Math.min(size.width/760,size.height/540)*.88;camera.updateProjectionMatrix()},[camera,size.width,size.height]);return null}
function WorkshopRobot({entry}){
 const pose=entry.telemetry?.position||entry.editor_position||[entry.mapping.origin_x,entry.mapping.origin_y,0];
 const scale=entry.mapping.scale,point=[(pose[0]-entry.mapping.origin_x)/scale-240,pose[2]/scale,(pose[1]-entry.mapping.origin_y)/scale];
 return <group position={point}>{entry.profile==='forward_only'?<><Box position={[0,.16/scale,0]} args={[.36/scale,.1/scale,.22/scale]} color="#467ca6"/>{[-1,1].map(side=><mesh key={side} position={[0,.08/scale,side*.15/scale]} rotation={[Math.PI/2,0,0]}><cylinderGeometry args={[.08/scale,.08/scale,.05/scale,16]}/><meshStandardMaterial color="#243e50"/></mesh>)}</>:<Box position={[0,5,0]} args={[8,8,8]} color="#467ca6"/>}<Label position={[0,24,0]} text={`${entry.name} · ${entry.telemetry?'LAST SIM POSE':'EDITOR / UNVERIFIED'}${entry.profile!=='forward_only'?' · POSE PROXY':''}`}/></group>
}
function Scene({scenario,robot,workshop}){
 const {world,blocked,ambulances}=scenario;
 const pose=robot?.plan?.scenario_id===scenario.id?robot?.job?.telemetry?.position:null;
 const mapping=robot?.plan?.mapping;
 const measured=pose&&mapping?[(pose[0]-mapping.origin_x)/mapping.scale-240,(pose[1]-mapping.origin_y)/mapping.scale]:null;
 return <>
  <color attach="background" args={['#e8ecdd']}/><ambientLight intensity={1.7}/><directionalLight castShadow position={[150,350,180]} intensity={2.8} shadow-mapSize={[2048,2048]} shadow-camera-left={-400} shadow-camera-right={400} shadow-camera-top={350} shadow-camera-bottom={-350} shadow-camera-far={900} shadow-bias={-.0005}/>
  <Box position={[0,-5,0]} args={[620,9,400]} color="#d4dec4"/>
  <Box position={[0,-11,0]} args={[621,5,401]} color="#aabb9c"/>
  <gridHelper args={[600,20,'#c3cfb8','#cbd6be']} position={[0,.02,0]}/>
  {world.roads.map(r=><Road key={r.id} r={r} world={world} blocked={blocked.includes(r.kind)||blocked.includes(r.id)}/>)}
  {world.buildings.map(b=><Building key={b.id} b={b}/>)}
  {Array.from({length:16},(_,i)=><Tree key={i} x={-280+(i%8)*80} z={i<8?-171:170}/>)}
  {ambulances.map((a,i)=><React.Fragment key={a.id}>{a.mission?.points?.length>1&&<Line points={a.mission.points.map(p=>[p[0],3,p[1]])} color={blocked.length?'#ed8458':'#22785b'} lineWidth={4} dashed={false}/>}<Ambulance a={a} index={i}/></React.Fragment>)}
  {measured&&<group position={[measured[0],0,measured[1]]}><Box position={[0,8,0]} args={[8,5,14]} color="#357aa1"/>{[-3,3].flatMap(x=>[-5,5].map(z=><Box key={`${x}:${z}`} position={[x,3,z]} args={[2,6,2]} color="#28536b"/>))}<Label position={[0,28,0]} text="ROBOT · LAST SIM POSE"/></group>}
  {workshop?.robots?.filter(r=>r.scenario_id===scenario.id).map(r=><WorkshopRobot key={r.id} entry={r}/>)}
  <OrbitControls makeDefault target={[0,0,0]} minZoom={.45} maxZoom={2.8} maxPolarAngle={Math.PI/2.15}/>
 </>
}
class Boundary extends Component{state={error:false};static getDerivedStateFromError(){return {error:true}}render(){return this.state.error?<div className="canvas-fallback">3D view unavailable. WebGL is required.<br/>Routing and scenario controls remain available.</div>:this.props.children}}
export default function City({scenario,robot,workshop}){return <Boundary><Canvas shadows orthographic camera={{position:[480,420,480],zoom:1.05,near:1,far:2000}} dpr={[1,1.7]} gl={{antialias:true}}><AutoFit/><Scene scenario={scenario} robot={robot} workshop={workshop}/></Canvas></Boundary>}
