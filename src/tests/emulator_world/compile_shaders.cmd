@echo off
rem The shaders of emulator_world, compiled with the fxc of the Windows SDK into the headers target.cpp includes.
setlocal
set FXC=fxc
where fxc >nul 2>nul || for /f "delims=" %%f in ('dir /b /s "%ProgramFiles(x86)%\Windows Kits\10\bin\*fxc.exe" ^| findstr /i x64') do set FXC=%%f
cd /d "%~dp0"
"%FXC%" /nologo /T vs_4_0 /E vs /Vn SceneVertexShader /Fh scene_vs.h scene.hlsl || exit /b 1
"%FXC%" /nologo /T ps_4_0 /E ps /Vn ScenePixelShader /Fh scene_ps.h scene.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_1 /E vs /Vn Scene12VertexShader /Fh scene12_vs.h scene.hlsl || exit /b 1
"%FXC%" /nologo /T ps_5_1 /E ps /Vn Scene12PixelShader /Fh scene12_ps.h scene.hlsl || exit /b 1
"%FXC%" /nologo /T vs_2_0 /E vs9 /Vn Scene9VertexShader /Fh stages_vs9.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T ps_2_0 /E ps9 /Vn Scene9PixelShader /Fh stages_ps9.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_0 /E vs_point /Vn PointVertexShader /Fh stages_vs_point.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T gs_5_0 /E gs_square /Vn SquareGeometryShader /Fh stages_gs.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T ps_5_0 /E ps_color /Vn ColorPixelShader /Fh stages_ps_color.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T vs_5_0 /E vs_patch /Vn PatchVertexShader /Fh stages_vs_patch.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T hs_5_0 /E hs_patch /Vn PatchHullShader /Fh stages_hs.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T ds_5_0 /E ds_patch /Vn PatchDomainShader /Fh stages_ds.h stages.hlsl || exit /b 1
"%FXC%" /nologo /T cs_5_0 /E cs_fill /Vn FillComputeShader /Fh stages_cs.h stages.hlsl || exit /b 1
